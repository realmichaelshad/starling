from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .algorithm_model import algorithm_brief
from .performance import diagnose_account, timing_recommendations
from .account_model import infer_account_model
from .opportunities import assess_opportunity
from .safety import assess_context
from .schemas import DraftRequest, PostContext, StrategyNextRequest
from .strategy_source import enhancement_guidance
from .strategy_v2 import brief_for as strategy_v2_brief, get_strategy as get_strategy_v2
from .preferences import propose_preference
from .storage import (
    get_account_profile,
    get_campaign,
    get_goal,
    latest_bridge_context,
    list_campaigns,
    list_experiments,
    list_goals,
    list_knowledge_sources,
    performance_records,
    confirmed_preferences,
    confirm_preference,
    save_opportunity_assessment,
    save_strategic_state,
    save_strategy_message,
    recent_strategy_instructions,
    strategy_messages,
)


def _active_goal(identifier: str | None = None) -> dict | None:
    if identifier:
        return get_goal(identifier)
    return next((goal for goal in list_goals() if goal.get("status") == "active"), None)


def _active_campaign(identifier: str | None = None) -> dict | None:
    if identifier:
        return get_campaign(identifier)
    return next((campaign for campaign in list_campaigns() if campaign.get("status") == "active"), None)


def _experiment_assignment(campaign_id: str | None, seed: str) -> dict | None:
    active = [experiment for experiment in list_experiments() if experiment.get("status") == "running" and (not experiment.get("campaign_id") or experiment.get("campaign_id") == campaign_id)]
    if not active:
        return None
    experiment = active[0]
    variants = experiment.get("variants", [])
    if not variants:
        return None
    digest = int(hashlib.sha256(f"{experiment['id']}:{seed}".encode()).hexdigest()[:8], 16)
    return {"experiment_id": experiment["id"], "variable": experiment["variable"], "variant": variants[digest % len(variants)],
            "primary_metric": experiment["primary_metric"], "hypothesis": experiment["hypothesis"]}


def _knowledge_for(objective: str, limit: int) -> list[dict]:
    grade_order = {"A": 0, "high": 0, "B": 1, "moderate": 1, "C": 2, "low": 2, "unrated": 3}
    sources = list_knowledge_sources(tags=[objective], compatible_only=True)
    sources.sort(key=lambda item: (item.get("review_status") == "stale", grade_order.get(str(item.get("evidence_grade")), 4), item.get("updated_at") or ""))
    return [source for source in sources if source.get("review_status") != "stale"][:limit]


def _live_trends() -> list[dict] | None:
    """Account-scored, lifecycle-labeled trends — not the raw snapshot dump.
    The probes caught chat listing Bollywood/sports tags at an AI account
    because this used to return unranked captures."""
    try:
        from .radar import _rising_for_account
        ranked = _rising_for_account(limit=6)
        result = []
        for trend in ranked:
            entry = {"name": trend["name"], "lifecycle": trend.get("lifecycle"),
                     "account_relevance": trend.get("account_score"), "category": trend.get("category")}
            if trend.get("personal_history"):
                entry["personal_history"] = trend["personal_history"]
            result.append(entry)
        return result or None
    except Exception:
        return None


def _bandit_evidence(action: str) -> list[dict] | None:
    if action not in ("reply", "quote", "post"):
        return None
    try:
        from .bandit import bandit_summary
        return bandit_summary(action, limit=4) or None
    except Exception:
        return None


def strategy_brief(request: DraftRequest) -> dict:
    profile = get_account_profile()
    account = infer_account_model()
    goal = _active_goal(request.goal_id)
    campaign = _active_campaign(request.campaign_id)
    objective = request.strategy_mode if request.strategy_mode != "balanced" else (goal or {}).get("objective", "followers")
    if objective in ("profile_visits",):
        objective = "followers"
    timing = timing_recommendations(objective if objective in ("reach", "conversation", "resonance") else "reach", profile.get("timezone", "UTC"))
    knowledge = _knowledge_for(str(objective), 5)
    seed = request.context.context_id or request.context.post_id or request.context.text[:120]
    experiment = _experiment_assignment((campaign or {}).get("id"), seed)
    memory = (campaign or {}).get("memory", {})
    recent_angles = list(memory.get("recent_angles", []))[-8:]
    manual = get_strategy_v2()
    # Current topic focus is direct user intent, kept distinct from inferred
    # account pillars. The writer may prioritize it, but it still has to pass
    # the ordinary fact, safety, and originality gates.
    try:
        from .user_learning import active_topics
        topic_memory = active_topics(6)
    except Exception:
        topic_memory = []
    purpose = ("enhance" if request.enhancement_mode else "create") + f"_{request.action}" \
        if request.action in ("reply", "quote", "post") else "decision"
    return {
        "objective": objective,
        "strategy_v2": strategy_v2_brief(purpose) or None,
        "algorithm": algorithm_brief(
            "enhance" if request.enhancement_mode
            else "create_reply" if request.action in ("reply", "quote")
            else "create_post") or None,
        "banned_phrases": manual.banned_phrases or None,
        # No `length_target` (V1, 2026-09-21): the manual's "about 40-120 visible
        # characters" was one author's length, and it reached every customer's
        # prompt. Length comes from THIS account's measured style block.
        "audience": request.audience or profile.get("audience") or "the account's existing audience",
        "account_model": {key: account.get(key) for key in ("positioning", "content_pillars", "account_promise", "audience_interest_signals", "voice_profile", "confidence")},
        "goal": goal,
        "campaign": {key: campaign.get(key) for key in ("id", "name", "narrative", "content_pillars", "status")} if campaign else None,
        "recent_angles_to_avoid_repeating": recent_angles,
        "fatigue_score": float(memory.get("fatigue_score", 0.0) or 0.0),
        "timing": timing,
        "experiment": experiment,
        "knowledge": [{"title": item["title"], "url": item["url"], "claim": item["claim"], "evidence_grade": item["evidence_grade"]} for item in knowledge],
        "confirmed_preferences": [{"rule": item["rule"], "scope": item["scope"]} for item in confirmed_preferences(24)],
        "recent_chat_instructions": recent_strategy_instructions(12),
        "topic_memory": [{key: item.get(key) for key in ("topic", "priority", "confidence", "verified_posts")}
                         for item in topic_memory],
        "live_trends": _live_trends(),
        "bandit": _bandit_evidence(request.action),
        "composer_enhancement_guidance": enhancement_guidance(request.action) if request.action in {"reply", "quote", "post"} else None,
        "guardrails": list(dict.fromkeys([*(profile.get("constraints") or []), *((goal or {}).get("guardrails") or []),
            "No engagement bait, fabricated facts, harassment, or automatic publishing."])),
    }


def _next_moves_plan() -> dict | None:
    """Ranked plan from cached signals only — next_strategy must stay fast and
    offline-safe, so no model call happens here."""
    try:
        from .planner import next_moves
        return next_moves(llm=None)
    except Exception:
        return None


def next_strategy(request: StrategyNextRequest) -> dict:
    profile = get_account_profile()
    rows = performance_records()
    goal = _active_goal(request.goal_id)
    campaign = _active_campaign(request.campaign_id)
    objective = request.objective or (goal or {}).get("objective") or "followers"
    timing_objective = objective if objective in ("reach", "conversation", "resonance") else "reach"
    timing = timing_recommendations(timing_objective, profile.get("timezone", "UTC"), rows)
    diagnostics = diagnose_account(rows)
    bridge_payload = (latest_bridge_context("x_context") or {}).get("data") or {}
    latest_context = request.context.model_dump(mode="json") if request.context else bridge_payload.get("context", bridge_payload)
    opportunity: dict | None = None
    parsed_context = None
    if latest_context:
        try:
            parsed_context = PostContext.model_validate(latest_context)
            safety = assess_context(parsed_context)
            if safety.blocked:
                opportunity = {
                    "action": "skip", "score": 0.0, "headline": "Do not engage with this context",
                    "rationale": safety.reasons[0] if safety.reasons else "This context is not safe to use for drafting.",
                    "reasons": safety.reasons, "reaction_hypotheses": [], "downside": "Engaging could amplify harmful or private material.",
                    "timing": {"reason": "No response is recommended."}, "confidence": "strong", "should_draft": False,
                    "alternatives": ["Move on", "Keep the material private"],
                }
            else:
                opportunity = assess_opportunity(parsed_context, request.now)
            save_opportunity_assessment(parsed_context.model_dump(mode="json"), opportunity)
        except (TypeError, ValueError):
            opportunity = None
    if opportunity:
        action = f"opportunity_{opportunity['action']}"
        reason = str(opportunity["rationale"])
    elif not profile.get("audience") or not profile.get("content_pillars"):
        action, reason = "complete_profile", "Define the audience and content pillars before optimizing tactics; otherwise the system cannot judge strategic fit."
    elif diagnostics["modeled_samples"].get(timing_objective, 0) < 30:
        action, reason = "collect_measurements", "Import or capture more dated analytics. The current sample is too small for a defensible performance forecast."
    elif campaign:
        action, reason = "advance_campaign", f"Advance the active campaign “{campaign['name']}” with a fresh angle from its content pillars."
    else:
        action, reason = "plan_original_post", "Create an original post tied to the active objective and a repeatable content pillar."
    evidence = _knowledge_for(str(objective), 3)
    try:
        from .user_learning import active_topics
        topic_memory = active_topics(6)
    except Exception:
        topic_memory = []
    action_kind = {
        "advance_campaign": "post",
        "plan_original_post": "post",
        "collect_measurements": "research",
        "complete_profile": "research",
    }.get(action, str((opportunity or {}).get("action") or "wait"))
    result = {
        "recommended_action": action, "reason": reason, "objective": objective,
        "goal": goal, "campaign": campaign, "timing": timing,
        "diagnostic_summary": diagnostics, "active_context": latest_context,
        "evidence": [{"title": item["title"], "url": item["url"], "claim": item["claim"], "evidence_grade": item["evidence_grade"]} for item in evidence],
        "confidence": (opportunity or {}).get("confidence", "moderate" if len(rows) >= 30 else "limited"),
        "recent_chat_instructions": recent_strategy_instructions(12),
        "topic_memory": [{key: item.get(key) for key in ("topic", "priority", "confidence", "verified_posts")}
                         for item in topic_memory],
        "live_trends": _live_trends(),
        "opportunity": opportunity,
        "principle": "Optimize for useful, original posts and measurable learning; virality cannot be guaranteed.",
        "plan": _next_moves_plan(),
        "next_action": {
            "kind": action_kind,
            "target_context": latest_context,
            "timing_window": (timing.get("windows") or [None])[0],
            "rationale": reason,
            "confidence": (opportunity or {}).get("confidence", "moderate" if len(rows) >= 30 else "limited"),
            "reaction_hypotheses": (opportunity or {}).get("reaction_hypotheses", []),
            "downside": (opportunity or {}).get("downside"),
            "reasons": (opportunity or {}).get("reasons", []),
            "should_draft": (opportunity or {}).get("should_draft", action_kind in ("reply", "quote", "post")),
            "alternatives": (opportunity or {}).get("alternatives", []),
        },
    }
    saved = save_strategic_state(result, reason=action)
    result["state_version"] = saved["id"]
    return result


def _delete_question_answer(message: str, context: dict | None) -> str | None:
    """'Should I delete this post?' — answer from the delete advisor when the
    user is looking at their own post; honest guidance otherwise."""
    lowered = message.lower()
    if "delete" not in lowered or not any(
            word in lowered for word in
            ("post", "tweet", "this", "it", "one", "them", "anything")):
        return None
    post_id = None
    if context and context.get("is_own_post") and context.get("post_id"):
        post_id = str(context["post_id"])
    elif re.search(r"\bmy (?:last|latest|most recent|newest) (?:post|tweet)\b"
                   r"|\bthe (?:last|latest) one\b", lowered):
        # HE NAMED IT. "Should I delete my last post" was answered with "open
        # the post you're considering deleting and ask again" — about the one
        # post the answer above can identify from storage without being shown
        # anything. Asking him to fetch what it already holds is the same
        # failure as asking him to state a goal he has just stated.
        try:
            from .storage import performance_records
            rows = [row for row in performance_records()
                    if str(row.get("post_id") or "").strip()]
            rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            post_id = str(rows[0]["post_id"]) if rows else None
        except Exception:                                    # noqa: BLE001
            post_id = None
    if post_id:
        try:
            from .performance import delete_advice
            advice = delete_advice(post_id)
        except Exception:
            advice = None
        if advice:
            lines = {"keep": "I'd keep it.", "reasonable": "Deleting it is defensible.",
                     "too_early": "Too early to delete.", "pause": "Pause before deleting.",
                     "insufficient_evidence": "I can't rank it honestly yet."}
            opener = lines.get(str(advice["verdict"]), "Here's the honest read.")
            return opener + " " + " ".join(advice["reasons"][:2])
    return ("Open the post you're considering deleting and ask again — I'll rank it against your comparable posts, "
            "check how fresh it is, and look at your own deletion history. One honest heads-up: people often delete "
            "underperformers by view count while the post is quietly earning replies, which the ranking values far more.")


_CHANGE_WORDS = re.compile(
    r"\b(gain(ed)?|lose|lost|grow|grew|growth|increase[ds]?|change[ds]?|"
    r"up or down|since|last (week|month|day)|this (week|month)|yesterday|"
    r"past (week|month|\d+ days?))\b", re.IGNORECASE)


def _asks_for_change(lowered: str) -> bool:
    """Is this a delta question rather than a level question?"""
    return bool(_CHANGE_WORDS.search(lowered))


def _follower_change_answer(lowered: str) -> str:
    """Follower CHANGE, which needs two snapshots that actually bracket a window.

    With one snapshot there is no delta to report, and saying the current count
    instead would answer a question the user did not ask (§1).
    """
    from .storage import list_profile_snapshots

    window_days = 7
    if re.search(r"\b(month|30 days?)\b", lowered):
        window_days = 30
    elif re.search(r"\b(yesterday|last day|24 ?h)\b", lowered):
        window_days = 1

    rows = [row for row in list_profile_snapshots(limit=400)
            if row.get("followers_count") is not None]
    if len(rows) < 2:
        have = rows[0]["followers_count"] if rows else None
        base = ("I only have one follower snapshot, so I cannot show a change yet"
                if rows else "I have no follower snapshots yet")
        tail = (f" (currently {have})." if have is not None else ".")
        return (base + tail +
                " Open your X profile with Starling running on a few different "
                "days and I will be able to answer this properly.")

    rows.sort(key=lambda row: str(row.get("captured_at") or ""))
    counts = [int(row["followers_count"]) for row in rows]
    current = counts[-1]
    baseline = counts[0]          # earliest observation in the whole series
    net = current - baseline
    low, high = min(counts), max(counts)

    span_days = 0
    try:
        span = (datetime.fromisoformat(str(rows[-1]["captured_at"]).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(rows[0]["captured_at"]).replace("Z", "+00:00")))
        span_days = max(1, round(span.total_seconds() / 86400))
    except Exception:
        span_days = 0

    window = f"over the ~{span_days} day(s) I've tracked" if span_days else "so far"
    if net > 0:
        line = f"You're at {current} followers, up {net} from {baseline} {window}."
    elif net < 0:
        line = f"You're at {current} followers, down {abs(net)} from {baseline} {window}."
    else:
        line = f"You're at {current} followers, about flat {window} (started and ended at {baseline})."
    # Report the FULL range so a dip-and-recovery is never hidden behind a flat
    # endpoint read — the account went 16 to 14 to 16, and "flat" alone misleads.
    if high - low >= 1 and (low != current or high != current):
        line += f" It has moved between {low} and {high} in that window."
    return line


def _format_window(window: Any) -> str:
    """A timing window as words, not a raw dict.

    `windows[0]` is `{'day':'Tuesday','start_hour':16,'end_hour':18,...}`, and
    interpolating it dumped that dict straight into the chat answer.
    """
    if isinstance(window, str):
        return window
    if not isinstance(window, dict):
        return str(window)

    def clock(hour: Any) -> str:
        try:
            h = int(hour)
        except (TypeError, ValueError):
            return str(hour)
        suffix = "AM" if h < 12 else "PM"
        display = h % 12 or 12
        return f"{display} {suffix}"

    day = window.get("day", "")
    start, end = window.get("start_hour"), window.get("end_hour")
    if day and start is not None and end is not None:
        return f"{day} {clock(start)}-{clock(end)}"
    return day or str(window)


# HE CORRECTED A FACT AND NOTHING HAPPENED. Told his most recent post was one
# he had already deleted, he said "but i deleted it" — and Starling dropped the
# thread and answered with the canned "Tell me the point you want to add…".
# A user correcting a factual claim is the strongest signal available: it is
# free supervision about the world, volunteered, and it was thrown away.
#
# PAST TENSE ONLY. "should i delete it" and "what should i delete" are advice
# questions with their own answer path (`_delete_question_answer`), and firing
# here would silently mark a live post dead. The verb has to have happened.
_DELETED_IT = re.compile(
    r"\b(?:i|i'?ve)\s+(?:already\s+|just\s+)?deleted\s+(?:it|that|this|them|"
    r"the\s+post|that\s+post|this\s+post|my\s+post|the\s+tweet|that\s+tweet)\b"
    r"|\b(?:it|that|that\s+post|this\s+post|the\s+post|that\s+tweet)\s+"
    r"(?:is|was|has\s+been|'?s)\s+(?:already\s+)?deleted\b"
    r"|\bi\s+deleted\s+it\b"
    # "i deleted the thor one" / "i deleted my kimi post" — same correction,
    # and it named WHICH post, which is the case the bare-pronoun forms above
    # cannot express. Matched here so the descriptor reaches the resolver.
    r"|\b(?:i|i'?ve)\s+(?:already\s+|just\s+)?deleted\s+(?:the|my|that|this)\s+"
    r"[\w'’-]+(?:\s+[\w'’-]+)?\s+(?:post|tweet|one)\b", re.IGNORECASE)


# THE TYPO WAS THE WHOLE DEFECT. The pattern above is structurally correct and
# had never once fired for him: he typed "but i dleted it", deletion is
# user-declared only, and so the post stayed counted forever — `deleted_at` was
# NULL on all 99 rows and `performance_records()` has been filtering on a column
# nothing ever set. One character, and the strongest supervision signal in the
# product was discarded every time it was offered.
#
# REPAIR THE WORD; DO NOT WIDEN THE PATTERN. Adding spellings as regex
# alternatives invites the next person to append "removed" — edit distance 4 and
# a DIFFERENT INTENT, since he may have removed a link and not the post. Deriving
# the accepted set from the word itself makes the boundary a property of the code
# instead of a property of whoever last edited a list.
#
# AND THE REAL INFLECTIONS HAVE TO BE EXCLUDED, which is the trap: "delete" is
# ALSO one edit from "deleted". Repairing it would rewrite "should i delete this"
# into "should i deleted this", which `_DELETED_IT` accepts — marking a live post
# dead on an advice question, precisely the failure its own comment warns about.
# So a token is repaired only when it is NOT already a real word.
_DELETE_FAMILY = frozenset({"delete", "deletes", "deleting", "deleted",
                            "deletion", "deletions", "deleter"})
_WORDS_RE = re.compile(r"[A-Za-z]+")


def _one_edit_from_deleted(word: str) -> bool:
    """One insertion, deletion or substitution away from "deleted" — no closer, no further."""
    target = "deleted"
    if word == target:
        return False
    short, long = (word, target) if len(word) <= len(target) else (target, word)
    if len(long) - len(short) > 1:
        return False
    if len(short) == len(long):
        return sum(1 for a, b in zip(short, long) if a != b) == 1
    index = 0
    while index < len(short) and short[index] == long[index]:
        index += 1
    return short[index:] == long[index + 1:]


def _repair_deleted_typo(message: str) -> str:
    """Restore a one-character slip in "deleted" so the structural matcher can see it."""
    def swap(match: "re.Match[str]") -> str:
        word = match.group(0)
        lowered = word.lower()
        if lowered in _DELETE_FAMILY:
            return word
        return "deleted" if _one_edit_from_deleted(lowered) else word
    return _WORDS_RE.sub(swap, message or "")


# THE UNNAMED CASE IS AN IRREVERSIBLE SILENT WRITE, AND THE TYPO REPAIR WIDENED
# ITS REACH. `mark_post_deleted` sets `deleted_at` and `mark_corpus_dirty()`
# propagates the loss into the voice and shape caches; there is no restore path
# anywhere in the engine. Replayed on a scratch copy, three unqualified
# sentences ("but i dleted it" / "i deleted it" / "i already deleted that one")
# marked THREE different posts dead, cascading backwards through the newest each
# time — each one ~1% of everything the product knows about him, and invisible
# until a number moves.
#
# So the unnamed case ASKS. A named one still writes: an explicit referent that
# resolves is the safe path and it is the case the resolver exists for.
#
# The slot is time-boxed and only a BARE affirmative claims it, because this
# function cannot see the conversation — `_facts_question_answer` is handed a
# message and nothing else. A five-minute window plus a four-word ceiling is
# narrow enough that an unrelated "yes" cannot reach a write, and every other
# message simply leaves the slot to expire.
_PENDING_DELETE: tuple[float, str, str] | None = None
_PENDING_DELETE_TTL_S = 300.0
_BARE_YES = re.compile(r"^\s*(yes|yeah|yep|yup|correct|confirm|do it|that one|"
                       r"the first|delete it)\b[\s.!]*$", re.IGNORECASE)


def _claim_pending_delete(message: str):
    """Return the post awaiting confirmation if THIS message confirms it."""
    global _PENDING_DELETE
    if not _PENDING_DELETE:
        return None
    stamped, post_id, snippet = _PENDING_DELETE
    if time.monotonic() - stamped > _PENDING_DELETE_TTL_S:
        _PENDING_DELETE = None
        return None
    if len(str(message or "").split()) > 4 or not _BARE_YES.match(message or ""):
        return None
    _PENDING_DELETE = None
    return post_id, snippet


def _deletion_correction(message: str) -> str | None:
    """"but i deleted it" — confirm which post, then mark it gone.

    Returns None when the message is not that correction, so every other chat
    path is untouched.
    """
    confirmed = _claim_pending_delete(message)
    if confirmed:
        post_id, snippet = confirmed
        try:
            from .storage import mark_post_deleted
            marked = mark_post_deleted(post_id)
        except Exception:                               # noqa: BLE001
            marked = False
        return (f'Marked "{snippet}" as deleted; I won\'t count it again.'
                if marked else
                f'I couldn\'t update the record for "{snippet}". Nothing was changed.')
    # Repaired ONCE, then used for every read below — the guard, the named-post
    # resolver and the descriptor all have to see the same string, or a typo
    # would pass the guard and then fail to name which post he meant.
    message = _repair_deleted_typo(message or "")
    if not _DELETED_IT.search(message):
        return None
    try:
        from .storage import mark_post_deleted, performance_records
    except Exception:                                   # noqa: BLE001
        return None
    try:
        rows = [row for row in performance_records() if str(row.get("text") or "").strip()]
    except Exception:                                   # noqa: BLE001
        return None
    if not rows:
        return ("Got it — noted as deleted. I don't have any other captured "
                "posts to fall back to, so re-sync when you get a chance.")
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    # WHICH post did he delete? Defaulting to the newest is right for "but i
    # deleted it" straight after being told his most recent one — that is the
    # conversation this was written for. It is wrong the moment the post under
    # discussion was any other one: after "what's my best post", "i deleted
    # that" marked the most RECENT post dead and left the deleted one counting.
    # Nothing here can see the previous turn, but when the message names the
    # post ("i deleted the thor one") that is a referent, and the resolver that
    # answers "my Kimi K3 post" elsewhere already knows how to find it.
    gone = rows[0]
    resolved = False
    try:
        from .account_facts import _mentioned_posts
        named = _mentioned_posts(message, rows, limit=1)
        if named and named[0].get("created_at"):
            match = next((row for row in rows
                          if row.get("created_at") == named[0]["created_at"]), None)
            if match is not None:
                gone, resolved = match, True
    except Exception:                                   # noqa: BLE001
        pass
    # HE NAMED ONE AND IT WAS NOT FOUND — DO NOT GUESS. Falling back to the
    # newest post here is not a near miss, it is marking a live post dead and
    # telling him it is handled: "i deleted the pancake recipe post" struck out
    # his most recent reply, which he never mentioned and had not deleted. A
    # descriptor that resolves to nothing is a question, not a default.
    descriptor = re.search(
        r"\bdeleted\s+(?:the|my|that|this)\s+([\w'’-]+(?:\s+[\w'’-]+)?)\s+"
        r"(?:post|tweet|one)\b", message or "", re.IGNORECASE)
    _GENERIC = {"last", "latest", "recent", "most recent", "newest", "previous", "other"}
    if descriptor and not resolved:
        word = descriptor.group(1).strip()
        # "I deleted the LATEST post" is an explicit referent — it says which
        # one, relationally, and does not depend on a previous turn. The hazard
        # the confirmation exists for is a bare "it"/"that", where the code's
        # own comment admits it cannot see what was being discussed. So a
        # generic-but-explicit referent resolves and writes directly.
        if word.lower() in _GENERIC:
            resolved = True
        else:
            snippets = [re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:52]
                        for row in rows[:3]]
            options = "; ".join('"' + snippet + '"' for snippet in snippets)
            return (f"I can't find a post matching “{word}” — I'd rather ask "
                    f"than mark the wrong one dead. Your three most recent are: "
                    f"{options}. Which one?")
    snippet = re.sub(r"\s+", " ", str(gone.get("text") or "")).strip()[:70]
    # UNNAMED: he said "I deleted it" and never said which. Today this wrote to
    # the newest post and reported it as done. Ask instead — the descriptor is
    # already in hand, and the only thing that changes is whether the sentence
    # reports a completed write or requests one.
    if not resolved:
        global _PENDING_DELETE
        _PENDING_DELETE = (time.monotonic(), str(gone.get("post_id") or ""), snippet)
        return (f'Just to be sure before I change anything — do you mean '
                f'"{snippet}"? Say yes and I\'ll stop counting it.')
    marked = False
    try:
        marked = mark_post_deleted(str(gone.get("post_id") or ""))
    except Exception:                                   # noqa: BLE001
        marked = False
    lead = (f'Thanks. Marked "{snippet}" as deleted; I won\'t count it again.'
            if marked else
            f'Thanks — I\'ll stop treating "{snippet}" as live.')
    remaining = [row for row in rows[1:] if str(row.get("post_id") or "")
                 != str(gone.get("post_id") or "")]
    if not remaining:
        return lead + " That was the last one I had captured, so there's nothing behind it."
    following = re.sub(r"\s+", " ", str(remaining[0].get("text") or "")).strip()[:70]
    return lead + f' Your most recent one now is "{following}".'


def _wanted_post_type(lowered: str) -> str | None:
    """Which KIND he asked about, when he said so.

    "What was my latest post" answered with his most recent REPLY, because the
    newest row of any type won and only the label changed. To him a reply is
    not a post — it lives somewhere else, it reads differently, and he asked
    for one thing and got another. None means he didn't specify, and newest-of
    -anything stays right.
    """
    # Match on the NOUN, not on an adjacency. Requiring "last|latest" or "my"
    # immediately before it meant "my best reply" and "my worst reply" carried
    # no type at all — an adjective in between was enough to lose it — and the
    # answer came back as his best POST. Callers have already established that
    # the message is about his own captured content, so a plain noun scan is
    # safe here; reply and quote win over post because they are the specific
    # words and "post" doubles as the generic one.
    if re.search(r"\brepl(?:y|ies)\b", lowered) and not re.search(
            r"\breply(?:ing)? to\b|\bshould i repl(?:y|ies)\b|\bwrite a reply\b",
            lowered):
        return "reply"
    if re.search(r"\bquote(?:-?tweets?|s)?\b", lowered):
        return "quote"
    if re.search(r"\b(?:posts?|tweets?)\b", lowered):
        return "post"
    return None


def _critique_recent_post(facts: dict, *, roast: bool, message: str = "") -> str:
    """A real read of the most recent post from its numbers - never a bare quote.

    "what do you think of my last post" used to return the post text and view
    count with no judgement. This compares the post's actual views to the
    account median and adds one measured factor, so the answer is grounded and
    never the model's "I can't assist".
    """
    try:
        from .account_facts import _plain_views
        from .performance import diagnose_post, performance_insights, performance_records
    except Exception:
        return "I couldn't read your post history just now - try again in a moment."
    rows = [row for row in performance_records() if row.get("text")]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    if not rows:
        return "I don't have any captured posts to look at yet."
    post = rows[0]
    # HONOUR THE KIND HE ASKED FOR. Newest-of-anything meant "what was my
    # latest post" returned his most recent reply with the label swapped.
    wanted = _wanted_post_type((message or "").lower())
    if wanted:
        match = next((row for row in rows
                      if str(row.get("post_type") or "post") == wanted), None)
        if match is not None:
            post = match
        elif wanted != "post":
            noun = {"reply": "replies", "quote": "quote-tweets"}[wanted]
            return f"I have no {noun} captured yet — only standalone posts."
    fmt = str(post.get("post_type") or "post")
    fmt = fmt if fmt in ("reply", "quote", "post") else "post"
    fmt_label = {"reply": "reply", "quote": "quote-tweet", "post": "post"}[fmt]
    # WHOLE post, and WHEN. It was clipped at 90 characters — a third of a
    # tweet, cut mid-word — and carried no date at all, so "what was my most
    # recent post" answered with a severed sentence and no way to tell whether
    # it went out this morning or last Tuesday.
    from .account_facts import _post_line, _display_timezone
    from .storage import get_account_profile
    line_facts = _post_line(post, _display_timezone(get_account_profile()))
    snippet = line_facts.get("text") or ""
    when = line_facts.get("posted_ago")
    views = _plain_views(post)
    # Compare like-for-like: a standalone post against the standalone median,
    # not the account-wide median (which replies skew upward). This is also what
    # keeps the number consistent with the diagnosis answer.
    try:
        median = ((performance_insights().get("by_format") or {}).get(fmt) or {}).get("median_views")
    except Exception:
        median = None
    if median is None:
        median = facts.get("median_views")
    lead = f'Your last {fmt_label}' + (f" ({when})" if when else "")
    parts = [f'{lead}: "{snippet}".']
    if views is not None and median:
        views, median = int(views), int(median)
        if views >= median * 1.3:
            parts.append(f"{views} views against your median {fmt_label} of {median} - a strong one."
                         if not roast else f"{views} views vs your {median} median {fmt_label}. Good, but don't get cocky.")
        elif views <= median * 0.7:
            parts.append(f"{views} views against your median {fmt_label} of {median} - it underperformed."
                         if not roast else f"{views} views against a {median}-view median {fmt_label}. It flopped. Learn and move on.")
        else:
            parts.append(f"{views} views, about your median {fmt_label} of {median} - middle of the pack.")
    elif views is not None:
        parts.append(f"It has {int(views)} views; I don't have enough history yet to say if that's good.")
    else:
        parts.append("It has no measured views yet, so I can't judge how it did.")
    try:
        diag = diagnose_post(str(post.get("post_id")), rows) if post.get("post_id") else None
        if diag and diag.get("likely_factors"):
            parts.append(str(diag["likely_factors"][0].get("evidence") or "").strip())
    except Exception:
        pass
    return " ".join(part for part in parts if part)


def _best_post_in_window(days: int, label: str) -> str:
    """Top post by views inside a window — his data, ranked, not refused."""
    try:
        from datetime import datetime, timedelta, timezone
        from .account_facts import _display_timezone, _parse_dt, _plain_views, _post_line
        from .storage import get_account_profile, performance_records
    except Exception:                                        # noqa: BLE001
        return "I couldn't read your post history just now — try again in a moment."
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = [row for row in performance_records()
            if str(row.get("text") or "").strip()
            and _plain_views(row) is not None
            and (_parse_dt(row.get("created_at")) or cutoff) >= cutoff]
    if not rows:
        return (f"I don't have any posts with view counts in the {label} "
                "to rank.")
    top_row = max(rows, key=lambda row: _plain_views(row) or 0.0)
    top = _post_line(top_row, _display_timezone(get_account_profile()))
    line = (f"Your best in the {label} is {top['views']} views"
            + (f" ({top['posted_ago']})" if top.get("posted_ago") else "")
            + f": “{top['text']}”")
    if len(rows) > 1:
        runner = sorted(rows, key=lambda row: _plain_views(row) or 0.0,
                        reverse=True)[1]
        line += (f" Next best was {int(_plain_views(runner) or 0)} views, "
                 f"across {len(rows)} measured posts in that window.")
    return line


def _best_of_type(wanted: str | None) -> dict | None:
    """Top-viewed row, restricted to a kind when he named one.

    Returns None to mean "use the precomputed fact"; a dict with
    `missing_type` to mean "he asked for a kind I have none of".
    """
    if not wanted:
        return None
    try:
        from .account_facts import _display_timezone, _plain_views, _post_line
        from .storage import performance_records
    except Exception:                                        # noqa: BLE001
        return None
    rows = [row for row in performance_records()
            if str(row.get("text") or "").strip()
            and _plain_views(row) is not None
            and str(row.get("post_type") or "post") == wanted]
    if not rows:
        return {"missing_type": wanted} if wanted != "post" else None
    top = max(rows, key=lambda row: _plain_views(row) or 0.0)
    line = _post_line(top, _display_timezone(get_account_profile()))
    line["type"] = wanted
    return line


def _worst_measured_post(facts: dict, message: str = "") -> str | None:
    """The genuinely lowest post, ranked by views — never the latest one.

    "What's my worst post" returned the most RECENT post, because the critique
    rule matches "is ... my ... post". Showing him his latest under the word
    "worst" asserts something about it that was never measured.
    """
    try:
        from .account_facts import _display_timezone, _plain_views, _post_line
        from .storage import get_account_profile, performance_records
    except Exception:                                        # noqa: BLE001
        return None
    rows = [row for row in performance_records()
            if str(row.get("text") or "").strip() and _plain_views(row) is not None]
    # Restricted to the kind he named, and labelled by what actually won —
    # min() over posts AND replies printed under the word "post" is the same
    # fault as "your best post", pointing the other way.
    wanted = _wanted_post_type((message or "").lower())
    if wanted:
        typed = [row for row in rows
                 if str(row.get("post_type") or "post") == wanted]
        if typed:
            rows = typed
        elif wanted != "post":
            noun = {"reply": "replies", "quote": "quote-tweets"}[wanted]
            return f"I have no measured {noun} to rank yet."
    if not rows:
        return None
    tz_name = _display_timezone(get_account_profile())
    worst_row = min(rows, key=lambda row: _plain_views(row) or 0.0)
    worst = _post_line(worst_row, tz_name)
    kind = {"reply": "reply", "quote": "quote-tweet"}.get(
        str(worst_row.get("post_type") or "post"), "post")
    line = (f"Your lowest {kind} on record is {worst['views']} views"
            + (f" ({worst['posted_ago']})" if worst.get("posted_ago") else "")
            + f": “{worst['text']}”")
    # COMPARE LIKE WITH LIKE, AND COUNT WHAT WAS ACTUALLY RANKED. A reply was
    # measured against the standalone-post median and the tail read "ranked
    # across the 4 posts that have view counts" while ranking four replies.
    median = None
    if wanted == "post" or wanted is None:
        median = facts.get("median_views")
    else:
        try:
            from .performance import performance_insights
            median = ((performance_insights().get("by_format") or {})
                      .get(kind if kind != "quote-tweet" else "quote")
                      or {}).get("median_views")
        except Exception:                                    # noqa: BLE001
            median = None
    if median:
        # `by_format` medians come back as floats, so this read "488.0 views".
        line += (f" Your median {kind} is {int(round(float(median)))}, so this "
                 "one landed well under it.")
    if len(rows) < len(performance_records()):
        plural = {"reply": "replies", "quote-tweet": "quote-tweets"}.get(kind, "posts")
        line += (f" Ranked across the {len(rows)} {plural} that have view "
                 "counts; the rest have no metrics to rank by.")
    return line


def _post_type_counts(facts: dict, lowered: str) -> str:
    """How many posts / replies / quotes are on record.

    "How many replies have I sent" was routed to the DRAFTING prompt, because
    the word "reply" reads as an instruction to write one. It is a count, and
    every row needed to answer it is already stored.
    """
    try:
        from .storage import performance_records
    except Exception:                                        # noqa: BLE001
        return "I couldn't read your post history just now — try again in a moment."
    rows = performance_records()
    # A PERIOD IN THE QUESTION IS PART OF THE QUESTION.
    #
    # This counted the whole archive and ignored every timeframe, so "how many
    # posts did I publish in august" answered 95 against a true 6 — a real number
    # answering a question nobody asked, which is the shape the views branch of
    # `_facts_question_answer` was already fixed for ("How many views did I get
    # this week" once returned the nearest number relabelled as a weekly total).
    # Measured on his live archive, 2026-08-05:
    #
    #     in august   -> answered 95,  true 6
    #     this week   -> answered 95
    #     in july     -> answered 95,  true 54
    #     replies in july -> answered 6, true 6   <- right BY ACCIDENT
    #
    # That last one is the reason this matters more than the arithmetic: every
    # reply he has is in July, so the wrong method produced the right number and
    # nothing looked broken.
    window, window_label = _count_window(lowered)
    if window is not None:
        rows = [row for row in rows if _created_within(row, window)]
    counts = {"post": 0, "reply": 0, "quote": 0}
    for row in rows:
        kind = str(row.get("post_type") or "post").lower()
        kind = "post" if kind in ("post", "original") else kind
        if kind in counts:
            counts[kind] += 1
    total = sum(counts.values())
    asked = ("reply" if re.search(r"\brepl(?:y|ies)\b", lowered)
             else "quote" if "quote" in lowered else None)
    # A timeframe was asked for and could not be resolved: answer all time and SAY
    # so, rather than let a lifetime number pass as a period one.
    suffix = ("" if window or not window_label else
              f" That is all time — I can't break it down by {window_label} yet.")
    scope = f" in {window_label}" if window else ""
    if asked:
        label = {"reply": "replies", "quote": "quote-tweets"}[asked]
        if window and not total:
            return f"You have no {label} on record{scope}."
        return (f"You have {counts[asked]} {label} on record{scope} "
                f"(out of {total} captured posts{scope}).{suffix}")
    if window and not total:
        return f"You have nothing on record{scope}."
    return (f"On record{scope}: {counts['post']} standalone posts, {counts['reply']} "
            f"replies, {counts['quote']} quote-tweets; "
            f"{total} in total.{suffix}")


_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def _count_window(lowered: str) -> tuple[tuple[str, str] | None, str]:
    """((from_iso_prefix, to_iso_prefix) | None, human label).

    Returns the label even when the window cannot be resolved, so the caller can
    admit the period was asked for instead of silently answering a different
    question. Deliberately small: the periods people actually type, and honesty
    for everything else.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    # HIS CALENDAR, NOT UTC. The first version of this built month and day
    # boundaries in UTC, which is the same mistake `_display_timezone` was
    # written for — its docstring: "reported 'you posted ~19:24 UTC on Monday'
    # for a post the user made on SUNDAY evening; the UTC calendar day had
    # already rolled over."
    #
    # For a month count it means any post made in the EVENING on the last day of
    # a month lands in the following one. Zero of his 60 dated posts hit that
    # today, so nothing was wrong yet — but his measured peak is 16:00 local and
    # there is a post at 20:06, so it was a matter of which day he posted on.
    # Rolling windows ("last 7 days") are the same instant in any zone; only the
    # calendar boundaries below need the local frame.
    # Imported here because `_display_timezone` is a local import in every other
    # function in this file that uses it — it is not module-scope, and assuming it
    # was would have been a NameError on the first month question.
    # `account_facts._zone`, not a bare ZoneInfo with a UTC except-branch.
    # `_display_timezone` returns a Windows DISPLAY name ("Eastern Daylight Time")
    # whenever the profile's timezone is unset or "UTC" — every user before their
    # profile is built — and ZoneInfo cannot parse that, so this landed on
    # `timezone.utc` and built calendar month and day boundaries in UTC. That is
    # precisely the bug the comment above says this code exists to avoid, and it
    # was silent because the label printed alongside still said the local zone.
    from .account_facts import _display_timezone, _zone as _resolve_zone

    zone: Any = _resolve_zone(_display_timezone(get_account_profile()))
    now = datetime.now(zone)
    # Rolling windows first — "this month" is a period phrase and must not be
    # mistaken for the calendar month named below.
    if re.search(r"\b(this|last|past)\s+week\b|\blast\s+7\s+days\b|\bthis\s+7\b", lowered):
        start = now - timedelta(days=7)
        return (start.isoformat(), now.isoformat()), "the last 7 days"
    if re.search(r"\b(this|last|past)\s+month\b|\blast\s+30\s+days\b", lowered):
        start = now - timedelta(days=30)
        return (start.isoformat(), now.isoformat()), "the last 30 days"
    if re.search(r"\b(today)\b", lowered):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.isoformat(), now.isoformat()), "today"
    if re.search(r"\byesterday\b", lowered):
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return ((end - timedelta(days=1)).isoformat(), end.isoformat()), "yesterday"
    for index, name in enumerate(_MONTHS, start=1):
        if re.search(rf"\b{name}\b", lowered):
            year_match = re.search(r"\b(20\d\d)\b", lowered)
            year = int(year_match.group(1)) if year_match else now.year
            start = datetime(year, index, 1, tzinfo=zone)
            end = (datetime(year + 1, 1, 1, tzinfo=zone) if index == 12
                   else datetime(year, index + 1, 1, tzinfo=zone))
            return (start.isoformat(), end.isoformat()), f"{name.title()} {year}"
    # A period was clearly meant but is not one of the above.
    if re.search(r"\b(this|last|past)\s+(year|quarter|fortnight)\b|\bsince\b|\brecently\b|\blately\b", lowered):
        return None, "that period"
    return None, ""


def _created_within(row: dict, window: tuple[str, str]) -> bool:
    """PARSED, not string-compared.

    Lexicographic comparison happens to work on his rows
    ("2026-07-01T18:44:07.000Z" against an isoformat bound) and breaks silently
    the first time a row arrives with a space instead of the "T", a missing
    offset, or a local timezone — and the failure mode is a count that is quietly
    wrong rather than an error. `performance.parse_datetime` already normalises
    every shape this database holds, including the trailing Z.
    """
    from .performance import parse_datetime

    created = parse_datetime(row.get("created_at"))
    if created is None:
        return False
    start, end = parse_datetime(window[0]), parse_datetime(window[1])
    if start is None or end is None:
        return False
    return start <= created < end


def _diagnose_performance(facts: dict) -> str:
    """Honest 'why aren't my posts doing well / what's working' from real numbers."""
    try:
        from .performance import performance_insights
        insights = performance_insights()
    except Exception:
        return "I couldn't read your performance data just now - try again shortly."
    by_format = insights.get("by_format") or {}
    post = by_format.get("post") or {}
    strongest = insights.get("strongest_format")
    windows = insights.get("best_windows") or []
    parts: list[str] = []
    median_views = post.get("median_views")
    # THE SAMPLE BEHIND THE MEDIAN, not the number of rows on file. This said
    # "across 89 recorded (solid sample)" about a median computed from 50 measured
    # posts — the other 39 have no view count at all.
    count = post.get("measured_views", post.get("posts", 0))
    if median_views is not None:
        parts.append(f"Your standalone posts run a median of {int(median_views)} views "
                     f"across {count} measured ({post.get('evidence', 'limited')} sample).")
    if strongest and strongest != "post":
        best = by_format.get(strongest) or {}
        if best.get("median_views") is not None:
            fmt = {"reply": "replies", "quote": "quote-tweets"}.get(strongest, strongest)
            # EVERY FIGURE CARRIES ITS SAMPLE — `performance_insights`' own
            # docstring promises it, and this line broke the promise where it
            # mattered most. It read "Your replies do better - a median of 588
            # views - so more of those is the clearest lever" off SIX replies,
            # with no number attached, as the headline recommendation of the
            # whole answer. A strategy change is exactly the wrong place to drop
            # the sample size.
            best_count = best.get("measured_views", best.get("posts", 0))
            thin = str(best.get("evidence") or "") != "solid"
            line = (f"Your {fmt} do better - a median of {int(best['median_views'])} "
                    f"views across {best_count} measured")
            if thin:
                # Named as a direction, not a lever, when the evidence is thin.
                parts.append(line + f" ({best.get('evidence')} sample). "
                             f"That is worth trying, but it is too few to call a lever yet.")
            else:
                parts.append(line + " - so more of those is the clearest lever.")
    if windows:
        parts.append(f"Your strongest measured window is {_format_window(windows[0])}; "
                     f"posting well off it costs reach.")
    if count < 8:
        parts.append("It's a small sample though, so read this as a direction, not proof.")
    if not parts:
        return ("I don't have enough measured posts yet to diagnose this honestly. "
                "Keep posting and I'll have real numbers to work from.")
    return " ".join(parts)


# A question about the FUTURE, deliberately narrow. Kept beside the metric
# pattern below because neither is safe alone: this half matches ordinary advice
# ("what will help me grow") and that half matches almost every account question
# ever typed. See the guard in `_facts_question_answer` for the measurements.
_PREDICTIVE_RE = re.compile(
    r"\b(?:will\s+i\s+(?:have|get|hit|reach|be\s+at)"
    r"|am\s+i\s+going\s+to\s+(?:have|get|hit|reach)"
    r"|will\s+my\s+(?:next|new)\s+\w*\s*(?:post|reply|quote|tweet)?"
    r"|how\s+many\s+\w+\s+will\b"
    r"|predict\s+(?:my|the)\b"
    r"|what\s+(?:should\s+i\s+expect|will\s+i\s+(?:have|get))\b"
    r"|(?:next|by)\s+(?:week|month|year)\b[^?]*\bwill\b)")
_PREDICTIVE_METRIC_RE = re.compile(
    r"\b(?:follower|followers|view|views|like|likes|repost|reposts|reply|replies"
    r"|impression|impressions|reach|growth|engagement|post|posts)\b")


def _facts_question_answer(message: str) -> str | None:
    """Deterministic answers for account-fact questions — used when the model
    is unavailable or over the time guard, so a timeout never turns a factual
    question into a vague pep talk."""
    # (helpers for the follower-change case live just below this function)
    # A CORRECTION COMES FIRST. It is a statement about the world, not a
    # question about it, so none of the intent matching below applies — and
    # answering it as though it were a question is exactly what went wrong.
    correction = _deletion_correction(message)
    if correction:
        return correction
    # MATCH THE TYPO-NORMALISED COPY TOO. Every intent below was written
    # against clean input and matched `message.lower()` raw, while the greeting,
    # scope and capability routers all matched a normalised copy. Typing the way
    # he actually types — "waht was my mroe recnt psot", "whats my raech",
    # "am i psoting to much" — missed all of them and fell through to the model,
    # which answered "your reach is limited" and "you're posting 3 times a day".
    # Both are fabrications, and both are answers this function already had.
    #
    # The normalised copy is for MATCHING ONLY and is never shown or sent to the
    # model; a token is only ever corrected toward a known routing word, so this
    # cannot invent an intent the message does not resemble. Joined with the raw
    # text so an intent that matched before still matches, exactly as before.
    try:
        from .text_normalizer import normalize_typos
        normalized = normalize_typos(message)
    except Exception:                                        # noqa: BLE001
        normalized = ""
    lowered = message.lower()
    if normalized and normalized != lowered:
        lowered = f"{lowered}\n{normalized}"
    # A QUESTION ABOUT THE FUTURE IS NOT A QUESTION ABOUT NOW.
    #
    # Measured 2026-08-05 against his live archive: "how many followers will I
    # have next month" answered "You have 23 followers in the latest local
    # snapshot", "how many followers am I going to have" answered the whole
    # account summary, and "what will my next post get" answered the LAST post's
    # views. Every one is a real number silently answering a question nobody
    # asked, which is the same shape `_count_window` exists to prevent for
    # timeframes ("in august -> answered 95, true 6").
    #
    # The honest phrasing already lives in this file — "I can't promise numbers;
    # nobody can see X's ranking" is what the sibling question about follower
    # gain returns. It was simply never applied to the future tense.
    #
    # BOTH halves are required to fire. "will" on its own is ordinary advice
    # vocabulary ("what will help me grow"), so a metric noun has to be present
    # too. Calibrated on all 1,102 of his real chat messages: ZERO matches, and
    # against a hand list of 16 real questions it stays quiet on every one while
    # catching 9 of 9 predictive phrasings, his spelling included.
    if _PREDICTIVE_RE.search(lowered) and _PREDICTIVE_METRIC_RE.search(lowered):
        return ("I can't tell you that - nobody can see how X will rank a post, "
                "and I'm not going to put a number on your future and call it a "
                "measurement. What I can do is tell you where you actually are "
                "(\"how many followers do I have\", \"am I growing\") or what to "
                "do next (\"what should I post about\") - both answered from your "
                "own measured history.")
    # Growth ADVICE ("how do i get more followers") must not return the current
    # count. It asks how to grow, not the number.
    wants_growth = bool(re.search(r"\b(get|gain|grow|growing|more|increase|build)\b", lowered)) \
        and bool(re.search(r"\bfollower|\baudience|\breach\b|\bgrow\b", lowered))
    # "am i growing?" is a DELTA question. The honest answer is that there is no
    # prior snapshot to compare - which is exactly what the model fabricated
    # ("+2 since yesterday").
    wants_growing = bool(re.search(r"\b(am i|are we|been) (growing|gaining)\b", lowered)) \
        or lowered.strip() in ("am i growing", "growing?", "am i growing?")
    # A CRITIQUE of a specific post ("what do you think of my last post", "was
    # it good", "roast it") must give a real read from the numbers, not the bare
    # quote wants_recent used to return, and never the model's refusal.
    # "WHEN was my last post" is a fact about the past, and it was being read as
    # "when SHOULD I post" — the timing rule matches any message with "when" and
    # "post" in it. He asked what time he last posted and was told, wrongly,
    # that there was not enough history to name a good window.
    wants_last_post_time = bool(re.search(
        r"\bwhen (?:was|did) (?:my|i)\b.{0,20}\b(post|tweet)\b"
        r"|\bwhen did i (?:last )?(?:post|tweet)\b"
        r"|\bwhat time did i (?:post|tweet)\b"
        r"|\bhow long (?:ago|since) .{0,20}(?:post|tweet)", lowered))
    # "What's my WORST post" answered with the most RECENT one, because the
    # critique rule matches "is ... my ... post". Being shown your latest post
    # under the word "worst" reads as a claim that it IS your worst.
    wants_worst = bool(re.search(
        r"\b(worst|lowest|weakest|flopp?ed most|least viewed)\b.{0,15}"
        r"\b(post|tweet|one|repl(?:y|ies)|quote(?:-?tweet)?)\b"
        r"|\bwhich post (?:did|does) (?:the )?worst\b", lowered))
    # "How many replies have I sent" is a COUNT. It was routed to the drafting
    # prompt ("a reply needs the post you're replying to…") because the word
    # "reply" is read as an instruction to write one.
    # "How many views did I get this week" was answered "4,886 views this week
    # — that's your best post ever": the nearest number in the facts, relabelled
    # as a weekly total. "What's my reach" got "your median reach score is 576",
    # a metric this system does not have and a number that appears nowhere in
    # its input. Both are the model filling a gap, and the gap was real.
    wants_period_views = bool(re.search(
        r"\b(?:how many|how much|total)\b.{0,20}\b(views?|impressions?|reach)\b"
        r".{0,25}\b(this|last|past|in the)\b.{0,12}\b(week|month|days?|7|30)\b"
        r"|\b(views?|impressions?)\b.{0,15}\b(this|last|past) (week|month)\b", lowered))
    wants_reach = bool(re.search(
        r"\b(what'?s|whats|what is|hows|how'?s) my reach\b|\bmy reach\b", lowered)) \
        and not wants_period_views
    # "Am I posting too much" / "what time do I usually post" — descriptions of
    # his own history, both answered from the wrong place: the first reported
    # his daily BUDGET as his rate, the second answered when he SHOULD post and
    # claimed there was not enough history, with 36 posts on file.
    # "to much" is how he writes "too much", and the normaliser will not fix it
    # — "to"/"too" is a real word either way, so correcting it would be a guess
    # about meaning rather than about spelling.
    # "is that good" after a number. The model answered "No. 22 followers and
    # 36 posts is a small account. You're not growing." — while `follower_trend`
    # in the same payload said +5 over 4.9 days. It is a judgement question, so
    # the model reached for a judgement and contradicted its own input; it is
    # also answerable from the record, which is the only version that cannot.
    # WHO HE IS. "What's my name" spent 43 seconds in the model and came back
    # "I don't have your name. You're the one who sets it on your profile." —
    # over a stored profile holding his handle, his display name and his bio.
    wants_own_identity = bool(re.search(
        r"\bwhat(?:'?s| is)? my (?:name|handle|username|@|display name)\b"
        r"|\bwho am i\b|\bwhats my bio\b|\bwhat(?:'?s| is) my bio\b"
        r"|\bwhat do you know about me\b", lowered))
    # NAMING THE NUMBER IS THE SAME QUESTION. The bare form ("is that good") was
    # caught; "is 23 followers good" was not, and it went to the model, which
    # answered from nothing — measured live 2026-08-05: "No, it's not great yet.
    # You have only twenty-three people watching your work... That number needs to
    # grow before you can rely on X for anything serious." Eight seconds, no
    # figure from the record, and an external judgement Starling cannot make.
    #
    # The handler below is the honest version and already existed: it reports the
    # measured follower trend and says outright that it can only compare him to
    # his own history. The bug was purely that this phrasing never reached it.
    #
    # Kept anchored and narrow. "is my post good" is a CRITIQUE (its own branch)
    # and must not be swallowed — the subject here has to be a countable the
    # record actually holds, with no possessive in front of it.
    _verdict_head = lowered.split("\n")[0]
    wants_verdict = bool(re.search(
        r"^\s*(?:so\s+|and\s+)?is (?:that|this|it) "
        r"(?:any )?(?:good|bad|a lot|normal|ok|okay|healthy|decent|fine|b#?ad)"
        r"\s*[?.!]*\s*$", _verdict_head)) or bool(re.search(
        r"\b(?:is|are)\s+(?:\d[\d,]*\s+)?"
        r"(?:followers?|posts?|tweets?|views?|likes?|repl(?:y|ies))\s+"
        r"(?:any\s+)?(?:good|bad|a lot|normal|ok|okay|healthy|decent|fine|low|high|enough)\b"
        r"|\b(?:is|are)\s+(?:that|this)\s+(?:many|a lot of)\s+"
        r"(?:followers?|posts?|views?)\b", _verdict_head))
    # "WHO IS MY AUDIENCE" IS AN ACCOUNT QUESTION, and nothing claimed it.
    #
    # `wants_growth` needs a growth verb, so this fell past every branch here to
    # `_web_lookup_answer` — measured live 2026-08-05, the answer was "I looked it
    # up: How to identify your target audience in 5 steps - Adobe (source:
    # adobe.com)". A generic marketing article, for a question about his own
    # account, from a product whose whole claim is that it reads HIS record.
    #
    # It is claimed here even though the honest answer is mostly a refusal, which
    # is the point: X exposes no follower demographics locally, so anything about
    # who his readers ARE would be invented. What IS measurable is what he posts
    # about, and whether he has told Starling an audience himself.
    wants_audience = bool(re.search(
        r"\b(?:who|what)(?:'?s| is| are)?\s+(?:my|the)\s+(?:target\s+)?"
        r"(?:audience|readers?|followers?|demographic)\b"
        r"|\bwho\s+(?:is\s+)?(?:reading|following|watching)\s+(?:me|my)\b"
        r"|\bwho\s+am\s+i\s+writing\s+for\b"
        r"|\bwho\s+(?:is\s+)?my\s+audience\b", lowered)) and not wants_growth
    wants_cadence = bool(re.search(
        r"\b(?:am i|do i)\b.{0,20}\b(post(?:ing)?|tweet(?:ing)?)\b.{0,20}"
        r"\b(too?\s+(?:much|often|many)|too little|enough|a lot)\b"
        r"|\bhow often do i (?:post|tweet)\b"
        r"|\bhow many (?:posts?|times) (?:a|per) day\b", lowered))
    # Any adverb, not a fixed list of five: "usaully" survives normalisation
    # (nothing in the routing vocabulary is one swap away), and an intent that
    # only fires on correctly spelled adverbs is an intent he cannot reach.
    wants_usual_time = bool(re.search(
        r"\bwhat time do i\b[^.?!\n]{0,15}?\b(?:post|tweet)\b"
        r"|\bwhen do i\b[^.?!\n]{0,15}?\b(?:usually|normally|typically|mostly|"
        r"usaully|normaly)\b[^.?!\n]{0,10}?\b(?:post|tweet)\b"
        r"|\bwhen do i (?:usually|normally|typically|mostly) (?:post|tweet)\b"
        r"|\bwhat (?:hours?|times?) do i post\b", lowered))
    wants_best_day = bool(re.search(
        r"\b(?:what|which|whats|what'?s) (?:was )?my best day\b"
        r"|\bbest day (?:i'?ve |i )?(?:ever )?had\b", lowered))
    wants_type_count = bool(re.search(
        r"\bhow many (posts?|replies|reply|quotes?|tweets?)\b.{0,25}"
        r"\b(have i|did i|do i have|i(?:'ve| have)?)\b"
        r"|\bhow many (posts?|replies|reply|quotes?|tweets?) (?:in )?(?:total|overall)\b"
        # A BARE follow-up. Mid-conversation he writes "and how many posts",
        # which carries no "have i" and so missed — and the model answered
        # "you've posted 36 times, 22 of those had measurable views", where 22
        # is the FOLLOWER count from the turn before. 34 have views.
        r"|\bhow many (posts?|replies|reply|quotes?|tweets?)\b\s*[?.!]*(?:\n|$)",
        lowered)) and "today" not in lowered and "follower" not in lowered
    # `reply` and `quote` are nouns here too. Without them "what was my latest
    # reply" matched no intent at all and fell to the model, while "what was my
    # latest post" was answered with that same reply — he could neither ask for
    # a reply nor avoid getting one.
    # "What's my best post" is a RANKING question, not a critique of the most
    # recent one. Widening the openers to accept "what's" let this swallow it
    # and answer with the newest reply. `wants_best` is computed further down,
    # so the exclusion is inline.
    wants_critique = (not wants_worst and not wants_last_post_time
                      and not re.search(r"\b(best|top|highest|worst|lowest)\b", lowered)) and (
        bool(re.search(
            # The 's' is OPTIONAL. Normalising "whats" drops it — "whats my
            # lsat rpely" becomes "what my last reply" — and requiring a
            # suffix meant his own spelling could never reach this.
            r"\b(what do you think|thoughts?|how('?s| is| was)|what('?s| is| was)?|"
            r"was|is|rate|review|roast|"
            r"grade|feedback on)\b.{0,30}\b(last|recent|latest|this|my)\b.{0,12}"
            r"\b(post|tweet|repl(?:y|ies)|quote(?:-?tweet)?)\b",
            lowered)) or bool(re.search(r"\broast (my|the|that|this|it)\b", lowered))
        or bool(re.search(r"\bwas (my|that|the|this) (last |recent |latest )?"
                          r"(post|tweet|reply|quote) (any )?good\b", lowered))
        # "What did I post last" carries no opener the pattern above knows and
        # spent 52 seconds in the model to reach an answer that is free.
        or bool(re.search(r"\bwhat did i (?:last )?(?:post|tweet|say)\b", lowered))
        # Bare "my last reply" — a REFERENCE to one he already sent, which was
        # read as a request to write one ("A reply needs the post you're
        # replying to. Attach it with the paperclip").
        or bool(re.search(r"^\s*my (?:last|latest|recent|newest)\s+"
                          r"(?:post|tweet|repl(?:y|ies)|quote)\s*[?.!]*\s*$",
                          lowered.split("\n")[0])))
    # A performance DIAGNOSIS ("why aren't my posts doing well", "what's
    # working") — the model refused ("I don't have access to your metrics")
    # despite being handed the data. Answer it from the real numbers.
    wants_diagnosis = bool(re.search(
        r"why.{0,30}(post|posts|tweet|reach|engagement|number|view).{0,30}"
        r"(well|good|bad|badly|poorly|work|working|doing|low|down|flop|struggl|tank)",
        lowered)) or bool(re.search(
        r"\b(what'?s|what is|whats) working\b|\bwhat works\b|\bwhat'?s not working\b"
        r"|\bposts? (not|arent|aren'?t) (doing|getting|performing)\b"
        r"|\bhow('?s| is) my (reach|engagement|performance)\b"
        r"|\btell me what('?s| is) working\b", lowered))
    # Honest "how do I go viral" — never a virality promise, real levers only.
    wants_viral = bool(re.search(r"\bgo(ing)? viral\b|\bblow (up|it up)\b|\bget big\b|\bblow up\b", lowered))
    # "What makes a good tweet" - PRINCIPLES, not a generated tweet.
    wants_tweet_advice = bool(re.search(
        r"\bwhat makes a (good|great|viral) (tweet|post)\b"
        r"|\bhow (do i|to) write a (good|great) (tweet|post)\b"
        r"|\btips? (for|on) (a )?(good )?(tweet|post|writing)\b", lowered))
    wants_recent = ("recent post" in lowered or "last post" in lowered or "latest post" in lowered) \
        and not wants_critique and not wants_diagnosis \
        and not any(word in lowered for word in ("delete", "roast", "why", "flop", "think", "good", "working"))
    # Level question ("how many followers do i have") — but NOT the advice form.
    wants_followers = (not wants_growth) and (
        (("follower" in lowered) and re.search(r"\bhow many\b", lowered))
        or lowered.strip().startswith("how many follower"))
    wants_today = "today" in lowered and "post" in lowered and re.search(r"\bhow many\b|\bcount\b", lowered)
    wants_date = bool(re.search(r"\bwhat (day|date) is (it|today)\b|\btoday'?s date\b", lowered))
    # The timing rule fires on any message containing "when"/"what time" and
    # "post", which is also the shape of every question about his own past
    # behaviour. Those are answered above, from measurements; this one is
    # advice about the future and must not swallow them.
    wants_timing = (not wants_last_post_time and not wants_usual_time
                    and not wants_cadence) and bool(
        re.search(r"\b(post|posts?|tweet)\b", lowered)) and bool(re.search(
        r"\b(when|what time|good time|best time|right time|now a good|time to post|"
        r"time to tweet|should i post now|post now)\b", lowered))
    # A STATUS QUESTION, NOT AN ADVICE ONE. "How can I make my account better"
    # matched this and got the bare summary — "Your account: 22 followers, 41
    # posts, a median of 306 views" — which answers "how is it doing" and not
    # the question he asked. Asking for improvement belongs to the review
    # capability, which diagnoses and then recommends.
    wants_summary = bool(re.search(r"\b(how('?s| is| am i)?)\b.*\b(account|doing|going)\b", lowered)) \
        and "post" not in lowered.split()[:2] \
        and not re.search(r"\b(better|improve|improv\w+|grow|bigger|fix|wrong|"
                          r"holding|stopping|should i do)\b", lowered)
    # `reply` and `quote` belong in the noun set here too — "what's my best
    # reply" matched nothing and went to the model, which has no ranking.
    wants_best = bool(re.search(
        r"\b(best|top|highest)\b.*\b(post|tweet|repl(?:y|ies)|quote(?:-?tweet)?)\b"
        r"|\bbest post\b|\bbest tweet\b|\bbest repl(?:y|ies)\b|\bbest quote\b",
        lowered))
    # "Which of my posts did best this month" is a real question about local
    # data. It reached the unverifiable-comparison guard ("which … best") and
    # came back "I won't rank models I can't verify" — a refusal to read his
    # own storage. Scoped to a window, so it is not the all-time answer above.
    wants_best_in_window = bool(re.search(
        r"\b(?:which|what)\b.{0,25}\b(?:posts?|tweets?|ones?)\b.{0,25}"
        r"\b(?:did best|performed best|got the most|did the best|went best)\b"
        r"|\b(?:best|top) posts? (?:this|last|in the past) (?:week|month)\b", lowered)) \
        or (wants_best and bool(re.search(r"\bthis (?:week|month)\b|\blast (?:week|month)\b", lowered)))
    if wants_best_in_window:
        wants_best = False
    # "How many LIKES did my last post get" was answered with the view count
    # and nothing else, because a genuine zero was filtered out as falsy.
    wants_last_post_metric = bool(re.search(
        r"\bhow many (likes?|replies|reposts?|retweets?|views?)\b.{0,30}"
        r"\b(?:last|recent|latest)\b.{0,12}\b(post|tweet|one)\b", lowered))
    wants_engagement = "engagement" in lowered and bool(re.search(r"\b(how|whats|what'?s|my)\b", lowered))
    wants_topics = bool(re.search(r"\bpost about most\b|\bwhat do i (post|tweet|talk) about\b"
                                  r"|\bmy niche\b|\bwhat'?s my niche\b|\bposted about most\b", lowered))
    if not (wants_recent or wants_followers or wants_today or wants_date or wants_timing
            or wants_growth or wants_growing or wants_summary or wants_best
            or wants_engagement or wants_topics or wants_critique or wants_diagnosis
            or wants_viral or wants_tweet_advice or wants_last_post_time
            or wants_worst or wants_type_count or wants_period_views or wants_reach
            or wants_cadence or wants_usual_time or wants_best_day
            or wants_best_in_window or wants_last_post_metric or wants_verdict
            or wants_own_identity or wants_audience):
        return None
    try:
        from .account_facts import account_facts
        facts = account_facts(message)
    except Exception:
        return None
    if wants_timing:
        windows = facts.get("best_windows") or []
        budget = facts.get("daily_post_budget")
        posted = facts.get("posts_today", 0)
        if not windows:
            # NO WINDOW IS NOT NO DATA. With 36 dated posts the timing engine
            # returns a real finding — "no hour in your 36 dated posts reliably
            # beats your own average; your results vary more by what you post
            # than by when" — and this branch threw it away for "I don't have
            # enough posting history yet", which is both false and less useful
            # than the thing it replaced.
            try:
                timing = timing_recommendations(
                    "reach", get_account_profile().get("timezone", "UTC"),
                    performance_records())
                measured = int(timing.get("sample_size") or 0)
                reason = str(timing.get("reason") or "").strip()
            except Exception:                                # noqa: BLE001
                measured, reason = 0, ""
            if measured >= 30 and reason:
                return (reason + " So don't wait for a slot — a timely reply "
                        "beats a well-timed post.")
            return ("I don't have enough posting history yet to name a best time. "
                    "Don't wait for a perfect slot - a timely reply beats a "
                    "well-timed post.")
        line = f"Your strongest measured window is {_format_window(windows[0])}."
        if isinstance(budget, int) and posted >= budget:
            line += f" You've already used your {budget}/day, so I'd hold off unless it's timely."
        else:
            line += " If now isn't that window, it's still fine - timeliness beats waiting."
        return line
    if wants_last_post_metric:
        recent = (facts.get("recent_posts") or [None])[0]
        if not recent:
            return "I don't have any captured posts for this account yet."
        asked = next((name for name in ("likes", "replies", "reposts", "views")
                      if re.search(rf"\b{name[:-1]}s?\b", lowered)), "views")
        value = recent.get(asked)
        when = recent.get("posted_ago") or ""
        if value is None:
            return (f"I don't have a {asked} count on record for your last post"
                    + (f" ({when})" if when else "") + " yet.")
        # A recorded ZERO is an answer. It was being dropped as falsy, so the
        # reply came back with the view count and never mentioned what he asked.
        parts = [f"Your last post{f' ({when})' if when else ''} has {value} {asked}"]
        others = [f"{recent[k]} {k}" for k in ("views", "likes", "replies", "reposts")
                  if k != asked and recent.get(k) is not None]
        if others:
            parts.append(" (" + ", ".join(others) + ")")
        return "".join(parts) + "."
    if wants_best_in_window:
        month = bool(re.search(r"\bmonth\b", lowered))
        return _best_post_in_window(30 if month else 7,
                                    "last 30 days" if month else "last 7 days")
    if wants_own_identity:
        # NO LOCAL IMPORT OF `get_account_profile` HERE. It is already imported
        # at module level, and re-importing it inside this function makes the
        # name local for the WHOLE function — so the timing branch above, which
        # runs first and calls it, died with UnboundLocalError, was swallowed
        # by its `except Exception`, and answered "I don't have enough posting
        # history" over 36 dated posts.
        profile = get_account_profile() or {}
        handle = str(profile.get("handle") or "").lstrip("@").strip()
        display = str(profile.get("display_name") or "").strip()
        bio = " ".join(str(profile.get("bio") or "").split())
        known: list[str] = []
        if display:
            known.append(display)
        if handle:
            known.append(f"@{handle}")
        if not known:
            return ("I don't have your handle stored yet — run a sync from "
                    "your profile page and I'll pick it up.")
        line = "You're " + (" (".join(known) + ")" if len(known) > 1 else known[0]) + "."
        if "bio" in lowered or "about me" in lowered:
            line += f" Your bio: “{bio}”" if bio else " I have no bio stored."
        elif bio:
            line += f" Your bio says: “{bio[:130]}”"
        return line
    if wants_audience:
        # WHAT CANNOT BE KNOWN IS SAID FIRST. X shows Starling no follower
        # demographics — no locations, ages or interests — so any description of
        # who his readers are would be invented. The measurable half is what he
        # posts about and whether he has set an audience himself.
        # NO LOCAL RE-IMPORTS HERE. Both names are already module-level (lines 10
        # and 18), and `from .storage import get_account_profile` inside this
        # function made the name local for the WHOLE function — so the verdict
        # branch below, which uses it 20 lines later, died with UnboundLocalError
        # on every question that did not take this path.
        stated = ""
        try:
            stated = str((get_account_profile() or {}).get("audience") or "").strip()
        except Exception:                                    # noqa: BLE001
            stated = ""
        pillars: list[str] = []
        try:
            pillars = [str(p) for p in (infer_account_model().get("content_pillars") or [])][:4]
        except Exception:                                    # noqa: BLE001
            pillars = []
        if stated:
            line = f"You've told me your audience is {stated}."
        else:
            line = ("I don't know who your audience is, and I can't find out — X "
                    "doesn't give me follower demographics, so anything I said "
                    "about who reads you would be made up. You can set it in your "
                    "profile and I'll write to it.")
        if pillars:
            line += (" What I can measure is what you post about: "
                     + ", ".join(pillars) + ".")
        return line
    if wants_verdict:
        parts: list[str] = []
        trend = facts.get("follower_trend") or {}
        followers = facts.get("followers")
        if trend.get("change") is not None and trend.get("over_days"):
            change, days = trend["change"], trend["over_days"]
            direction = ("up" if change > 0 else "down" if change < 0 else "flat")
            if change:
                parts.append(f"Followers are {direction} {abs(change)} over "
                             f"{days:g} days (~{trend['per_week']:g}/week)")
            else:
                parts.append(f"Followers are flat over {days:g} days")
        elif followers is not None:
            parts.append(f"You're at {followers} followers with no earlier "
                         "snapshot to compare against")
        median_views = facts.get("median_views")
        week = facts.get("views_last_7_days")
        if median_views is not None:
            line = f"your median post gets {median_views} views"
            if week is not None:
                line += f", {week:,} in the last 7 days"
            parts.append(line)
        if not parts:
            return ("I don't have enough captured history to judge that yet.")
        # NO EXTERNAL BENCHMARK EXISTS HERE. Starling can see one account. A
        # verdict against "accounts like yours" would be invented, and that is
        # how "you're not growing" got said over a payload showing +5.
        sentences = ". ".join(part[0].upper() + part[1:] for part in parts)
        return (sentences + ". I can only compare you to your own history — I "
                "have no other accounts to measure against, so I won't tell "
                "you where that ranks.")
    if wants_cadence:
        rate = facts.get("posts_per_day_measured")
        budget = facts.get("daily_post_budget")
        today = facts.get("posts_today", 0)
        if rate is None:
            return ("I don't have enough dated posts to measure how often you "
                    "post yet.")
        window = facts.get("rate_window_days")
        # MEASURED rate first, budget second, and never one wearing the other's
        # label. "You're posting 3 times a day, which is exactly your daily
        # budget" was the budget reported as the measurement.
        line = (f"You're averaging {rate:g} posts a day over the last "
                f"{window:g} days. Today: {today}.")
        if isinstance(budget, int):
            if rate > budget:
                line += (f" That's above the {budget}/day your own numbers "
                         "support — the extra ones are most likely splitting "
                         "the same attention.")
            elif rate < budget * 0.5:
                line += (f" Your measured budget is {budget}/day, so there's "
                         "room if you have something worth posting.")
            else:
                line += f" That's in line with the {budget}/day your history supports."
        return line
    if wants_usual_time:
        hours = facts.get("usual_hours_local") or []
        if not hours:
            return "I don't have enough dated posts to see a posting pattern yet."
        top = ", ".join(f"{h['hour']} ({h['posts']} posts)" for h in hours)
        return (f"You mostly post around {top}, in your local time. "
                "That's when you DO post, not necessarily when you should — "
                "ask about your best window for that.")
    if wants_best_day:
        day = facts.get("best_day_by_views")
        if not day:
            return ("I don't have enough posts with view counts to name a "
                    "best day yet.")
        return (f"Your best day by views was {day['weekday']} {day['date']}: "
                f"{day['views']:,} views across {day['posts']} post(s).")
    if wants_period_views:
        month = bool(re.search(r"\bmonth\b|\b30\b", lowered))
        label, days = ("last 30 days", "30") if month else ("last 7 days", "7")
        views = facts.get(f"views_last_{days}_days")
        posts = facts.get(f"posts_last_{days}_days", 0)
        measured = facts.get(f"posts_measured_last_{days}_days", 0)
        if not posts:
            return f"You have no posts on record in the {label}."
        if views is None:
            return (f"You posted {posts} time(s) in the {label}, but none of "
                    "them has a view count on record yet, so I can't total them.")
        line = f"{views:,} views across the {label}, from {posts} post(s)"
        if measured < posts:
            line += f". {measured} of them have view counts; the rest have none yet"
        return line + "."
    if wants_reach:
        median_views = facts.get("median_views")
        week = facts.get("views_last_7_days")
        if median_views is None:
            return ("I don't have enough posts with view counts yet to read "
                    "your reach.")
        # Name what the number IS. There is no "reach score" in this system,
        # and inventing one is how a made-up 576 got stated as a measurement.
        # SAY WHAT THE POOL IS. `median_views` and `posts_with_measured_views`
        # cover posts AND replies — 38 rows, 4 of them replies — and this
        # called them "views per post across 38 measured posts". The median
        # happens to be identical either way in his data, which is exactly why
        # a wrong label survives: nothing looks off until the day it diverges.
        measured = facts.get("posts_with_measured_views")
        line = (f"Reach here just means views — there's no score behind it. "
                f"Your median is {median_views} views across "
                f"{measured} measured posts and replies")
        if week is not None:
            line += f", and {week:,} views in the last 7 days"
        best = facts.get("best_post_ever") or {}
        if best.get("views") is not None:
            kind = {"reply": "reply", "quote": "quote-tweet"}.get(
                str(best.get("type") or "post"), "post")
            line += f". Your ceiling so far is {best['views']} on one {kind}"
        return line + "."
    if wants_last_post_time:
        # SAME MIX-UP, SECOND HANDLER. "When was my last post" reported his
        # last REPLY's timestamp, because this took recent_posts[0] of any
        # type and only swapped the noun. Fixing the critique answer alone
        # would have left the identical wrong answer one phrasing away.
        candidates = facts.get("recent_posts") or []
        wanted = _wanted_post_type(lowered)
        recent = next((row for row in candidates
                       if str(row.get("type") or "post") == wanted), None) \
            if wanted else None
        if recent is None and wanted and wanted != "post":
            noun = {"reply": "replies", "quote": "quote-tweets"}[wanted]
            return f"I have no {noun} captured yet — only standalone posts."
        if recent is None:
            recent = candidates[0] if candidates else None
        if not recent:
            return "I don't have any captured posts for this account yet."
        when = recent.get("posted_local")
        ago = recent.get("posted_ago")
        line = f"Your last {recent.get('type') or 'post'} went out"
        line += f" {ago}" if ago else ""
        line += f", {when}." if when else "."
        posted_today = facts.get("posts_today", 0)
        if not posted_today:
            line += " Nothing today yet."
        return line
    if wants_worst:
        worst = _worst_measured_post(facts, lowered)
        if not worst:
            return ("I don't have enough posts with view counts to name a worst "
                    "one — no post on record has metrics to rank by.")
        return worst
    if wants_type_count:
        return _post_type_counts(facts, lowered)
    if wants_critique:
        return _critique_recent_post(facts, roast="roast" in lowered, message=lowered)
    if wants_diagnosis:
        return _diagnose_performance(facts)
    if wants_viral:
        windows = facts.get("best_windows") or []
        tip = f" Post in your strongest window ({_format_window(windows[0])})." if windows else ""
        return ("I can't promise viral - nobody can see X's ranking, and anyone who says "
                "otherwise is guessing. What actually moves reach: reply under bigger "
                "accounts in your lane while their post is still fresh, make one clear point "
                "per post, and give people a concrete reason to follow." + tip)
    if wants_tweet_advice:
        try:
            from .performance import performance_insights
            strongest = performance_insights().get("strongest_format")
        except Exception:
            strongest = None
        base = ("A good tweet makes ONE clear point and leads with it - no wind-up. It reads "
                "in a single pass, uses plain words, and takes a real position instead of a "
                "safe one. Specifics beat vague takes.")
        if strongest:
            fmt = {"reply": "replies", "quote": "quote-tweets", "post": "standalone posts"}.get(strongest, strongest)
            base += f" On your own numbers, {fmt} measure strongest, so lean on that format."
        return base
    if wants_growing:
        # No prior follower snapshot => no honest delta. Say so; never invent one.
        return _follower_change_answer(lowered)
    if wants_growth:
        windows = facts.get("best_windows") or []
        tip = (f" Post in your strongest window ({_format_window(windows[0])}) when you can."
               if windows else "")
        return ("Reply under bigger accounts in your lane while the post is fresh - "
                "that's where new people actually find you." + tip +
                " I can't promise numbers; nobody can see X's ranking.")
    if wants_summary:
        followers = facts.get("followers")
        total = facts.get("total_posts_recorded")
        median = facts.get("median_views")
        parts = []
        if followers is not None:
            parts.append(f"{followers} followers")
        if total:
            parts.append(f"{total} posts recorded")
        if median is not None:
            # Precise scope: the median is over the posts that HAVE view counts,
            # not all recorded posts, and it spans all formats (the critique
            # answer reports the per-format median separately). Overstating it as
            # "across all your posts" was the one thing the final audit caught.
            measured = facts.get("posts_with_measured_views")
            scope = f"across the {measured} posts I have views for" if measured else "across your measured posts"
            parts.append(f"a median of {median} views {scope}")  # MEDIAN, not average
        if not parts:
            return "I don't have enough captured history yet to summarize the account."
        line = "Your account: " + ", ".join(parts) + "."
        # DIRECTION, not just level. "22 followers" answers how many and leaves
        # the only question he actually has — is this going anywhere — to be
        # guessed, when two snapshots on record already answer it.
        trend = facts.get("follower_trend") or {}
        if trend.get("change") is not None and trend.get("over_days"):
            change, days = trend["change"], trend["over_days"]
            if change > 0:
                line += (f" Followers are up {change} over the last "
                         f"{days:g} days (~{trend['per_week']:g}/week).")
            elif change < 0:
                line += (f" Followers are down {abs(change)} over the last "
                         f"{days:g} days.")
            else:
                line += f" Followers are flat over the last {days:g} days."
        best = facts.get("best_post_ever") or {}
        if best.get("views") is not None:
            line += f" Best post so far: {best['views']} views."
        return line
    if wants_best:
        # SAME FAULT AS "LATEST POST", ONE RANKING AWAY. `best_post_ever` is a
        # max() over posts AND replies with no type filter, and this prints the
        # word "post" over whatever wins. His #3 by views is a reply at 3,569
        # against a #1 post at 4,931 — the day that reply overtakes, "your best
        # post" names a reply. Latent is not fixed.
        best = _best_of_type(_wanted_post_type(lowered))
        if best is None:
            best = facts.get("best_post_ever")
            if not best or best.get("views") is None:
                return ("I don't have a measured best post yet - no post has a "
                        "view count on record.")
        if best.get("missing_type"):
            noun = {"reply": "replies", "quote": "quote-tweets"}[best["missing_type"]]
            return f"I have no measured {noun} to rank yet."
        when = best.get("posted_local") or ""
        text = (best.get("text") or "").strip()
        label = {"reply": "reply", "quote": "quote-tweet"}.get(
            str(best.get("type") or "post"), "post")
        line = f"Your best {label} has {best['views']} views"
        if when:
            line += f" (posted {when})"
        line += f": “{text}”" if text else "."
        return line
    if wants_engagement:
        median = facts.get("median_views")
        measured = facts.get("posts_with_measured_views")
        if median is None:
            return "I don't have enough posts with measured views yet to read your engagement."
        line = (f"Median views per post: {median}, across {measured} posts with "
                "metrics on record.")
        # This used to end "I don't have per-post likes and replies aggregated,
        # so I won't guess those" — a disclaimer about a sum that was never
        # performed, not about data that was missing. It is all on the same rows
        # the view count comes from.
        counted = facts.get("posts_with_interactions") or 0
        if counted:
            line += (f" Across those: {facts.get('total_likes', 0)} likes, "
                     f"{facts.get('total_replies', 0)} replies, "
                     f"{facts.get('total_reposts', 0)} reposts.")
            rate = facts.get("median_engagement_rate_pct")
            if rate is not None:
                line += (f" That is a median of {rate}% of viewers doing "
                         "something — views measure delivery, this measures "
                         "whether it landed.")
        return line
    if wants_topics:
        niche = facts.get("niche")
        if niche:
            # Already 'This account posts about: ...' - never claim a frequency
            # ranking, which the model invented ('mostly Anthropic, Fable a few').
            return niche + " I don't have per-topic counts, so I can't rank them."
        return "I don't have enough history to name your main topics yet."
    if wants_date:
        return f"It's {facts.get('now')}."
    if wants_recent:
        recent = (facts.get("recent_posts") or [None])[0]
        if not recent:
            return "I don't have any captured posts for this account yet."
        # Was `created_at[:10]` — the raw UTC calendar date, which is the exact
        # thing `posted_local` was precomputed to replace: after ~20:00 his
        # time the UTC day has already rolled over, so a Sunday-evening post
        # was reported as Monday.
        when = recent.get("posted_ago") or recent.get("posted_local") or ""
        line = f"Your most recent recorded {recent.get('type') or 'post'}"
        if when:
            line += f" ({when})"
        line += f": “{recent.get('text')}”"
        if recent.get("views") is not None:
            line += f", {recent['views']} views"
            if recent.get("interactions"):
                line += f", {recent['interactions']} interactions"
                # GUARDED ON THE RATE, NOT ON THE INTERACTIONS. `account_facts`
                # withholds the percentage below a denominator floor, because a
                # ratio on 3 views moves 33 points per interaction and reads as
                # a verdict. This branch is reached whenever there is at least
                # one interaction, so the old unconditional interpolation put
                # "(None%)" on screen for exactly the post that motivated the
                # floor: 3 views, 1 like, `engagement_rate_pct=None`.
                #
                # When the rate is withheld, say the count and stop. "3 views,
                # 1 interactions so far" is true and complete — the percentage
                # was the only part that was ever wrong.
                rate = recent.get("engagement_rate_pct")
                if rate is not None:
                    line += f" ({rate}%)"
            line += " so far."
        return line
    if wants_followers:
        # "How many did I GAIN last week?" is a different question from "how
        # many do I have". Answering the second when asked the first substitutes
        # an available number for an unavailable one, which is exactly what §1's
        # "unknown stays unknown" rule exists to prevent.
        if _asks_for_change(lowered):
            return _follower_change_answer(lowered)
        if facts.get("followers") is None:
            return "I don't have a captured follower count yet — open your X profile once so I can read it."
        return f"You have {facts['followers']} followers in the latest snapshot."
    return f"Your history shows {facts.get('posts_today', 0)} standalone post(s) dated today " \
           f"(daily budget: {facts.get('daily_post_budget')})."


def local_strategy_answer(message: str, context: dict | None = None, goal_id: str | None = None, campaign_id: str | None = None) -> str:
    lowered = message.lower()
    profile = get_account_profile()
    facts_answer = _facts_question_answer(message)
    if facts_answer:
        return facts_answer
    delete_answer = _delete_question_answer(message, context)
    if delete_answer:
        return delete_answer
    if lowered.strip(" .!?") in {"hey", "hi", "hello", "yo", "what's up", "whats up"}:
        return "Hey — I’m here. What are you trying to say on X right now? Give me the rough version; I’ll help you find the strongest move and keep it sounding like you."
    if "when" in lowered or "time" in lowered:
        goal = _active_goal(goal_id)
        objective = (goal or {}).get("objective", "reach")
        if objective not in ("reach", "conversation", "resonance"):
            objective = "reach"
        result = timing_recommendations(objective, profile.get("timezone", "UTC"))
        if not result["windows"]:
            return result["reason"]
        first = result["windows"][0]
        return f"Your strongest measured window is {first['day']} around {first['start_hour']:02d}:00–{first['end_hour']:02d}:00 {result['timezone']}. This is a correlation from {result['sample_size']} posts, so test it rather than treating it as a guarantee."
    if "why" in lowered and any(word in lowered for word in ("flop", "underperform", "engagement", "like", "repost")):
        return "I can compare the post with similar posts and identify likely factors, but I cannot know a viewer's private motivation. Open the post diagnosis for evidence, confidence, and a testable next change."
    if "viral" in lowered:
        return "No system can guarantee virality. I’ll optimize for a clear hook, an original useful idea, audience fit, credible sourcing, timing evidence, and a measurable experiment—without engagement bait."
    campaign = _active_campaign(campaign_id)
    if campaign:
        return f"The next useful move is to advance “{campaign['name']}” with a fresh angle, then measure the active goal. Keep one idea per post and change only one experimental variable."
    if context:
        author = str(context.get("author") or "this account")
        return f"I’m looking at {author}'s post with you. Tell me the point you want to add—even if it is rough—and I’ll tell you whether this is worth replying to, quoting, or leaving alone."
    return GENERIC_STRATEGY_FALLBACK


# The last resort when nothing above matched. Named so the caller can tell it
# apart from a real answer: when the model TIMED OUT rather than had nothing to
# say, returning this reads as a reply to a question he did not ask, with no
# sign that anything went wrong. See `_assist_chat_sync`.
GENERIC_STRATEGY_FALLBACK = (
    "I’m with you. Give me the rough idea you’re considering, and I’ll help you "
    "sharpen the point, choose the right move, and turn it into something you "
    "would actually post."
)


def begin_strategy_chat(message: str, conversation_id: str | None, context: dict | None, goal_id: str | None, campaign_id: str | None) -> tuple[str, list[dict]]:
    conversation_id = conversation_id or str(uuid4())
    save_strategy_message(conversation_id, "user", message, context)
    # A concrete correction such as "No, I want to write about local models"
    # is usable intent right now. Store it separately from permanent voice
    # rules, so it can guide the next draft without redefining the account.
    try:
        from .user_learning import learn_from_chat
        learn_from_chat(message, conversation_id)
    except Exception:
        pass
    # Direct, first-person instructions are useful durable signals. Persist
    # only those high-confidence rules locally; ordinary conversation remains
    # chat history rather than becoming an unwanted personality rule.
    preference = propose_preference(message)
    if preference and float(preference.get("confidence", 0.0)) >= 0.78:
        confirm_preference(None, message, str(preference["rule"]), str(preference["scope"]), True)
    history = strategy_messages(conversation_id, 20)
    return conversation_id, history


def finish_strategy_chat(conversation_id: str, answer: str, context: dict | None = None) -> None:
    save_strategy_message(conversation_id, "assistant", answer, context)
