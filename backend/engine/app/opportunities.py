from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .performance import timing_recommendations
from .schemas import PostContext
from .storage import confirmed_preferences, get_account_profile, performance_records


# A claim that is REPORTED rather than shown — the shape a reply would have to
# endorse without being able to check it.
#
# "today", "latest", "percent" and "%" used to sit in this list, which put a
# hard research gate (score 0.0, should_draft False, "Verify the premise before
# joining in") in front of half of this timeline: "OpenAI shipped structured
# outputs today" and "new evals show a 34% drop in tool-calling accuracy" were
# both refused. Neither needs verifying to reply to — one is an announcement,
# the other is an argument you engage on its mechanism. A bare date word and a
# percent sign are not a sourcing risk; second-hand reporting is.
CURRENT_CLAIM_SIGNALS = ("breaking", "just announced", "according to", "data shows",
                         "new study", "study finds", "sources say", "reportedly",
                         "leaked", "report claims")
# Reactions that are the WHOLE post. Matched against what is left after the
# reaction word, because these were prefix matches: "Nice writeup — but the
# batching numbers only hold if you keep the KV cache resident" was skipped as
# "mostly a low-substance greeting", and so was every post opening with
# "First", "Agree" or "Wow" before making its actual point.
LOW_VALUE_PATTERNS = ("gm", "good morning", "first", "nice", "wow", "so true", "agree", "thoughts?", "follow for follow")
# What is left over after the opener, below which the post really is just the
# reaction. A reply, not a rule of thumb: "Agree" plus eight words is a nod.
_LOW_VALUE_REMAINDER = 30
# Ad copy by its CALL TO ACTION. "giveaway" and "airdrop" were in here as bare
# substrings, so "the airdrop meta is dead because the incentive design rewards
# sybils" was skipped as a promotion — they are topics in his niche, not ads.
PROMOTIONAL_PATTERNS = ("buy my", "join my course", "join my cohort", "dm me for",
                        "link in bio", "enter the giveaway", "giveaway ends",
                        "claim your airdrop", "airdrop is live", "use code ")


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]{3,}", value.lower()) if token not in {"this", "that", "with", "from", "have", "will"}}


def _metric(context: PostContext, name: str) -> float:
    value = (context.visible_metrics or {}).get(name, 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _count(context: PostContext, name: str) -> int | None:
    """A raw count, or None when the key is absent — never a silent zero.

    `_metric()` returns 0.0 for a missing key and so cannot tell "no views" from
    "views unknown". Every unknown-stays-unknown rule in this module needs that
    distinction, so it lives here rather than in each caller.
    """
    metrics = context.visible_metrics or {}
    if name not in metrics:
        return None
    value = metrics.get(name)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _reply_count(context: PostContext) -> int | None:
    """Replies from either source the real captures use, absence preserved.

    Census of 72 real captures: 49 carry `visible_reply_count`, 23 carry only
    `visible_metrics["replies"]`. Reading one source alone loses a third of them.
    """
    seen = getattr(context, "visible_reply_count", None)
    if isinstance(seen, int):
        return seen
    return _count(context, "replies")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _reply_followup_line(replies: int) -> str:
    if replies == 1:
        return "1 reply already, and answering it beats starting a second post."
    return (f"{replies} replies already, and answering one of those beats "
            "starting a second post.")


def _post_evidence_line(context: PostContext, freshness: dict[str, Any] | None) -> str | None:
    """This post's own raw numbers — never a derived rate (see the rate floor).

    Cannot be wallpaper by construction: the numbers differ per post. Measured
    across the 66 real captures it produces 56 distinct lines, most common 12%,
    against the 87.7% single sentence it replaces.

    Returns None when every fact held is a zero. An earlier version printed
    "So far: 0 likes, 0m in." on the 4 captures carrying no views key — a
    sentence whose entire content is an absence.
    """
    parts: list[str] = []
    views, likes = _count(context, "views"), _count(context, "likes")
    reposts = _count(context, "reposts")
    if views is not None:
        parts.append(_plural(views, "view"))
    if likes is not None:
        parts.append(_plural(likes, "like"))
    if reposts:                      # non-zero only; three zeros reads as noise
        parts.append(_plural(reposts, "repost"))
    if not parts or all(part.startswith("0 ") for part in parts):
        return None
    body = ", ".join(parts)
    if freshness:
        age = _humanize_age(freshness["age_minutes"]).replace(" ago", "")
        return f"So far: {body}, {age} in."
    return f"So far: {body}."


def _preference_penalty(text: str) -> tuple[float, list[str]]:
    """Confirmed preferences, applied.

    These two checks looked for the EXACT strings "avoid rhetorical question"
    and "avoid hot take" — and the extractor has never produced either. Say
    "stop asking rhetorical questions" and it stores "Avoid asking rhetorical
    questions"; say "never use hot take" and it stores "Avoid use hot take".
    Neither contains the phrase being searched for, so every confirmed
    preference was echoed back to him as recorded and then changed nothing.

    Matched on the SHAPE now — an avoid-rule that mentions the subject — so any
    way he phrases it counts, which is the only version of this that survives
    contact with how people actually talk.
    """
    lowered = text.lower()
    penalties, notes = [], []
    for preference in confirmed_preferences():
        rule = str(preference.get("rule") or "").lower()
        prohibits = rule.startswith(("avoid", "never", "no ", "don't", "do not"))
        if not prohibits:
            continue
        if "rhetorical question" in rule and "?" in lowered:
            penalties.append(0.12)
            notes.append("Your confirmed preference avoids rhetorical-question openings.")
        if "hot take" in rule and "hot take" in lowered:
            penalties.append(0.18)
            notes.append("Your confirmed preference avoids ‘hot take’ framing.")
    return min(0.35, sum(penalties)), notes


def _today_posting_note(rows: list[dict[str, Any]], now: datetime) -> str | None:
    """Use only dated local records; never guess how often the user posted today.
    The threshold is the user's own measured daily budget (cadence curve),
    falling back to 3 when their history is too thin to measure."""
    # Resolved once, outside the loop, and shared with the same rule in
    # `timing._posts_today` — see the comment on the comparison below.
    # `account_facts._zone` handles the failure that actually happens: Windows
    # reports the machine zone as a display name ("Eastern Daylight Time") which
    # ZoneInfo cannot parse, and it falls back to the machine's real tzinfo rather
    # than to UTC. A bare ZoneInfo() here with a UTC except-branch would land back
    # on the UTC calendar day this fix exists to get away from.
    from .account_facts import _display_timezone, _zone as _resolve_zone
    local_zone = _resolve_zone(_display_timezone(get_account_profile() or {}))
    today_local = now.astimezone(local_zone).date()
    count = 0
    for row in rows:
        if str(row.get("post_type") or "").lower() not in {"original", "post"}:
            continue
        value = row.get("created_at")
        if not value:
            continue
        try:
            created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            # HIS CALENDAR DAY, not UTC's — the same fix as `timing._posts_today`,
            # and the same reason: a post at 20:06 local is already tomorrow in
            # UTC, so this note claimed posts on days he had not posted and
            # missed the ones he had. Both this and the budget gate read the same
            # number, so they have to agree about when "today" is.
            if created.astimezone(local_zone).date() == today_local:
                count += 1
        except ValueError:
            continue
    try:
        from .performance import daily_post_budget
        budget, evidence = daily_post_budget(rows)
    except Exception:
        budget, evidence = 3, None
    if count >= budget:
        note = f"Your history shows {count} standalone posts already dated today; a reply may be a cleaner next move than another original post."
        if evidence:
            note += f" Measured on this account: {evidence}."
        return note
    if count == budget - 1 and count >= 1:
        return (f"Your history shows {count} standalone post{'s' if count != 1 else ''} dated today; "
                "avoid adding another unless it has a clearly different audience payoff.")
    return None


def _is_only_a_reaction(lowered: str) -> bool:
    """Is the reaction the WHOLE post, or just how it opens?

    The old test was `startswith(pattern + " ")`, which cannot tell "Nice." from
    "Nice writeup — but the batching numbers only hold if you keep the KV cache
    resident." Both were skipped as low-substance greetings. What matters is
    what is left after the opener.
    """
    for pattern in LOW_VALUE_PATTERNS:
        if lowered == pattern:
            return True
        if lowered.startswith(pattern):
            rest = lowered[len(pattern):].lstrip(" ,.!?:;—-…\n")
            # Nothing but the reaction, or the reaction plus a few words.
            if len(rest) <= _LOW_VALUE_REMAINDER:
                return True
    return False


def _has_current_claim(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in CURRENT_CLAIM_SIGNALS)


def _conversation_stage(context: PostContext) -> str:
    replies, quotes = _metric(context, "replies"), _metric(context, "quotes")
    if replies >= 30 or quotes >= 12:
        return "active"
    if replies >= 5 or quotes >= 2:
        return "forming"
    return "early"


def _humanize_age(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))}m ago"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}h ago".replace(".0h", "h")
    return f"{minutes / (60 * 24):.1f}d ago".replace(".0d", "d")


def _humanize_rate(per_hour: float) -> str:
    if per_hour >= 1000:
        return f"~{per_hour / 1000:.1f}K/h".replace(".0K", "K")
    return f"~{int(round(per_hour))}/h"


def assess_freshness(context: PostContext, now: datetime) -> dict[str, Any] | None:
    """Age + velocity per the strategy manual §3.1: 'velocity matters more
    than age'. Rates are averages (cumulative totals ÷ age) — the honest proxy
    visible metrics allow — and are labeled as such."""
    raw = getattr(context, "post_created_at", None)
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    age_minutes = max(0.5, (now - created).total_seconds() / 60.0)
    age_hours = age_minutes / 60.0
    # A RATE NEEDS A DENOMINATOR. 21 of the 62 real captures carrying a views
    # key sit under the floor (34%), and every one of them printed a fabricated
    # rate: 2 views at 0 minutes reported as a ~240/h room, 16 views at 3
    # minutes classified "mid". The number is an artifact of dividing by
    # age_hours, and it drove a room tier, a -0.10 score penalty and a sentence
    # he reads.
    #
    # `_RATE_MIN_VIEWS` is imported, never copied — the same floor
    # `account_facts` uses to withhold an engagement percentage, for the same
    # reason. None rather than 0.0: a zero is a claim, and a None cannot be
    # used silently.
    from .account_facts import _RATE_MIN_VIEWS
    _views_total = _count(context, "views") or 0
    _rate_known = _views_total >= _RATE_MIN_VIEWS
    views_per_hour = (_metric(context, "views") / age_hours) if _rate_known else None
    replies_per_hour = (_metric(context, "replies") / age_hours) if _rate_known else None
    if age_minutes <= 30:
        bracket, note = "prime", "under 30 minutes old — the strongest reply window"
    elif age_minutes <= 90:
        bracket, note = "viable", "30-90 minutes old — still a good window"
    elif age_minutes <= 240:
        bracket, note = "needs_velocity", "90 minutes to 4 hours old — only worth it if the conversation is still moving"
    else:
        bracket, note = "stale", "over 4 hours old — the reply window is normally closed"
    # "Still hot" saves an old post: real average velocity, not vibes.
    still_hot = bool(_rate_known and (views_per_hour >= 20_000 or replies_per_hour >= 30))
    # THE AVERAGE CANNOT SEE DYING. Views ÷ age is cumulative: a post that did
    # 100K views in its first hour and nothing since still reads 20K/h at hour
    # five. The visible reply ages are the direct evidence — if the newest
    # visible reply is old, the thread went quiet no matter what the average
    # says, and a "still hot" earned on the average is revoked.
    reply_ages = [a for a in (getattr(context, "visible_reply_ages_minutes", None) or [])
                  if isinstance(a, (int, float)) and a >= 0]
    newest_reply_minutes = min(reply_ages) if reply_ages else None
    dying = (len(reply_ages) >= 3 and newest_reply_minutes is not None
             and newest_reply_minutes > 90)
    alive = newest_reply_minutes is not None and newest_reply_minutes <= 15
    if dying:
        still_hot = False
    # THE DERIVATIVE, NOT JUST THE SILENCE. A thread can be dying down while
    # still technically active: the visible replies' ages say how the gaps are
    # trending. Old half sparse, new half sparser = decelerating; tightening =
    # heating up. Needs enough replies to mean anything.
    trend = None
    if len(reply_ages) >= 6:
        ordered = sorted(reply_ages)                    # newest -> oldest
        newer, older = ordered[:len(ordered) // 2], ordered[len(ordered) // 2:]
        newer_span = max(newer) - min(newer)
        older_span = max(older) - min(older)
        newer_gap = newer_span / max(1, len(newer) - 1)
        older_gap = older_span / max(1, len(older) - 1)
        if newer_gap > older_gap * 1.8:
            trend = "decelerating"
        elif newer_gap * 1.8 < older_gap:
            trend = "accelerating"
        else:
            trend = "steady"
    # ROOM SHAPE. Replies-per-view separates a broadcast (huge audience, thin
    # thread — a reply is lost in the scroll) from a conversation (dense
    # thread relative to its size — replies are what people came to read).
    total_views = _metric(context, "views")
    total_replies = _metric(context, "replies") or float(
        getattr(context, "visible_reply_count", None) or 0)
    shape = None
    if total_views >= 1000 and total_replies > 0:
        density = total_replies / total_views
        shape = "conversation" if density >= 0.002 else (
            "broadcast" if density <= 0.0004 else "mixed")
    # CROWDING. Forty visible replies and you are late = buried under the fold;
    # few replies inside the window = the first-mover slot is still open.
    crowd = int(getattr(context, "visible_reply_count", None) or 0)
    crowded = crowd >= 25 and age_minutes > 90
    first_mover = crowd <= 3 and age_minutes <= 90
    # THE ROOM SIZE. A reply is read by the people who open this post — the
    # velocity IS the room. 200 views an hour is talking in an empty hallway;
    # an OpenAI-scale account fills a stadium. Tiers, not a curve, so the
    # decision text can say which room this is.
    if not _rate_known:
        room = "unknown"          # unmeasured, NOT quiet
    elif views_per_hour >= 50_000:
        room = "huge"
    elif views_per_hour >= 5_000:
        room = "big"
    elif views_per_hour >= 300:
        room = "mid"
    elif _metric(context, "views") > 0:
        room = "tiny"
    else:
        room = "unknown"
    stats_line = f"posted {_humanize_age(age_minutes)}"
    if _rate_known and _metric(context, "views") > 0:
        stats_line += f" · {_humanize_rate(views_per_hour)} views avg"
    if _rate_known and _metric(context, "replies") > 0:
        stats_line += f" · {_humanize_rate(replies_per_hour)} replies avg"
    if still_hot and bracket in ("needs_velocity", "stale"):
        stats_line += " · still accelerating"
    if dying:
        stats_line += f" · thread went quiet {_humanize_age(newest_reply_minutes).replace(' ago', '')} ago"
    elif alive:
        stats_line += f" · replies still arriving (newest {_humanize_age(newest_reply_minutes)})"
    if room in ("tiny", "huge"):
        stats_line += f" · a {_humanize_rate(views_per_hour)} room"
    if trend == "decelerating" and not dying:
        stats_line += " · replies thinning out"
    elif trend == "accelerating":
        stats_line += " · replies speeding up"
    if crowded:
        stats_line += f" · {crowd} replies already, crowded"
    elif first_mover:
        stats_line += " · first-mover window open"
    return {
        "age_minutes": round(age_minutes, 1),
        # None survives the round: the rate is UNKNOWN, and a 0.0 here would
        # be a claim. Every consumer below already branches on it.
        "views_per_hour": round(views_per_hour, 1) if views_per_hour is not None else None,
        "replies_per_hour": round(replies_per_hour, 1) if replies_per_hour is not None else None,
        "bracket": bracket,
        "still_hot": still_hot,
        "dying": dying,
        "alive": alive,
        "newest_reply_minutes": newest_reply_minutes,
        "room": room,
        "trend": trend,
        "shape": shape,
        "crowded": crowded,
        "first_mover": first_mover,
        "note": note,
        "stats_line": stats_line,
    }


def assess_opportunity(context: PostContext | None, now: datetime | None = None) -> dict[str, Any]:
    """Choose a useful X move before writing. This intentionally favors restraint over forced engagement."""
    profile = get_account_profile()
    now = now or datetime.now(timezone.utc)
    rows = performance_records()
    if context is None:
        timing = timing_recommendations("reach", profile.get("timezone", "UTC"), rows)
        cadence_note = _today_posting_note(rows, now)
        return {
            "action": "post",
            "score": 0.46,
            "headline": "Hold one useful original idea for the right moment",
            "rationale": "There is no live conversation selected. A focused original post is better than manufacturing an interaction.",
            "reasons": ["No current X context is available.", "A saved or researched idea can be developed without forcing a reply.", *([cadence_note] if cadence_note else [])],
            "reaction_hypotheses": ["People who already care about the topic may save or share a specific useful idea."],
            "downside": cadence_note or "Posting without a real audience payoff can add noise rather than build recognition.",
            "timing": timing,
            "confidence": "limited" if len(rows) < 30 else "directional",
            "should_draft": True,
            "alternatives": ["Research a timely topic", "Wait for a relevant conversation"],
        }

    text = context.text.strip()
    if context.visibility == "protected":
        return _wait("Keep this private context private", "Protected context can be discussed with Starling, but it is not a safe basis for public outreach.", ["Protected material is not uploaded or reused."], context)
    if context.is_own_post:
        # Live momentum against the account's own baseline turns the generic
        # "follow the discussion" into a situational read: a post that is
        # taking off deserves reply-answering NOW (reply-to-reply compounds),
        # a quiet one deserves patience — advice only, never automation.
        momentum = None
        freshness = assess_freshness(context, now)
        if freshness and freshness["age_minutes"] <= 6 * 60:
            try:
                from .learning_loop import account_velocity_baseline, momentum_verdict
                momentum = momentum_verdict(freshness["views_per_hour"], freshness["replies_per_hour"],
                                            account_velocity_baseline("post"),
                                            views=_metric(context, "views"))
            except Exception:
                momentum = None
        # THREE OF THE FOUR LINES HERE CLAIMED REPLIES EXIST. Measured on his 66
        # real captures, 57 have ZERO replies (86%) and the maximum anywhere is 2,
        # so the headline, the rationale and the second reason were all false on
        # the common case — the card advised him to attend to a conversation that
        # was not there. Conditioning only the reason would have left the other
        # two lying at the same rate.
        #
        # The zero-reply branch splits on AGE because a single else-sentence is
        # wrong on 44% of itself: 25 of those 57 are more than a day old, where
        # "give it room" is false advice and posting again is the correct move.
        replies_seen = _reply_count(context)
        reasons = ["The context belongs to your account."]
        if replies_seen is None:
            # Unknown stays unknown: no reply claim in either direction.
            headline = "Decide this one on what the post already did"
            rationale = ("This is your post. Read what it has done before adding "
                         "another; do not manufacture activity.")
        elif replies_seen > 0:
            headline = "Follow the discussion, not the metric"
            rationale = ("This is your post. A thoughtful follow-up to a substantive "
                         "reply can deepen the conversation; do not manufacture activity.")
            reasons.append(_reply_followup_line(replies_seen))
        else:
            # The window test reuses this module's OWN bracket rather than
            # importing analyzer's REPLY_NEEDS_MOVEMENT_MINUTES (not in scope
            # here) or restating 240: `assess_freshness` already classifies
            # anything past four hours as "stale".
            if freshness is not None and freshness["bracket"] != "stale":
                headline = "Nothing has come in on this one yet"
                rationale = ("This is your post and it has no replies yet, so there "
                             "is no conversation to deepen. Give it room before "
                             "adding another.")
            elif freshness is not None:
                headline = "This one is finished; the next post is the move"
                rationale = ("This is your post and it drew no replies in its window. "
                             "There is nothing here to follow up, so put the effort "
                             "into the next post rather than a second one on this.")
            else:
                headline = "No replies on this one"
                rationale = ("This is your post and it has no replies, so there is "
                             "no conversation to deepen.")
            evidence = _post_evidence_line(context, freshness)
            if evidence:
                reasons.append(evidence)
        if momentum and momentum["status"] == "taking_off":
            headline = "This is taking off — answer the best replies now"
            rationale = momentum["advice"]
            reasons = [f"Average velocity is ~{momentum['velocity_ratio']}x your own median for posts.",
                       *([momentum["reply_note"]] if momentum.get("reply_note") else []),
                       momentum["note"]]
        elif momentum and momentum["status"] == "quiet":
            reasons = [*reasons, momentum["advice"]]
        result = {
            "action": "follow_up",
            "score": 0.58,
            "headline": headline,
            "rationale": rationale,
            "reasons": reasons,
            "reaction_hypotheses": ["A specific follow-up can reward the people already engaging."],
            "downside": "Replying to every reaction can make the account feel performative.",
            "timing": {"reason": "Respond while the conversation remains active, only when you can add substance."},
            "confidence": "directional",
            "should_draft": False,
            "alternatives": ["Wait for a substantive question", "Save the insight for a later original post"],
        }
        if momentum:
            result["momentum"] = momentum
        if freshness:
            result["freshness"] = freshness
        return result
    if not text or len(text) < 18:
        return _wait("Skip this one", "There is not enough substance here to add a reply that serves your audience.", ["The visible post has too little usable context."], context)
    if bool(getattr(context, "reply_restricted", False)):
        return _wait("Skip this one", "Replies are restricted, so forcing a quote would create a weaker and less natural move.", ["The original author has limited replies."], context)
    lowered = text.lower().strip()
    if _is_only_a_reaction(lowered):
        return _wait("Skip this one", "This does not contain enough of an idea to improve for your audience.", ["The post is mostly a low-substance greeting, reaction, or engagement prompt."], context)
    if any(pattern in lowered for pattern in PROMOTIONAL_PATTERNS):
        return _wait("Skip this one", "This is primarily promotional. A public reply would be more likely to borrow attention than add value.", ["The post is a promotion rather than a conversation your audience needs."], context)

    # Local-only mode cannot verify time-sensitive factual claims. The strongest
    # move is to pause rather than turn an unverified premise into a confident take.
    if _has_current_claim(text) and not context.links:
        return {
            "action": "research",
            "score": 0.0,
            "headline": "Verify the premise before joining in",
            "rationale": "This post makes a current or numerical claim. Starling could not check it here, so it should not turn that premise into a confident public reply.",
            "reasons": ["The claim appears current, numerical, or reported.", "No inspectable source link is visible in the post."],
            "reaction_hypotheses": ["A careful response protects trust better than a fast but unsupported take."],
            "downside": "Reacting quickly could tie your account to a claim that later proves incomplete or wrong.",
            "timing": {"reason": "Wait until the factual premise can be checked."},
            "confidence": "strong",
            "should_draft": False,
            "alternatives": ["Ask a narrow source-seeking question", "Wait for a primary source"],
            "conversation_stage": _conversation_stage(context),
        }

    pillars = " ".join(profile.get("content_pillars") or [])
    audience = str(profile.get("audience") or "")
    topic_overlap = len(_tokens(text) & _tokens(f"{pillars} {audience}"))
    question_signal = 1.0 if "?" in text or any(word in text.lower() for word in ("how", "why", "what do you")) else 0.0
    stage = _conversation_stage(context)
    conversation_signal = min(1.0, (_metric(context, "replies") + _metric(context, "quotes") * 1.5) / 40.0)
    reach_signal = min(1.0, _metric(context, "views") / 100_000.0)
    novelty = 0.8 if len(text) > 80 else 0.55
    fit = min(1.0, 0.25 + topic_overlap * 0.2 + question_signal * 0.2 + novelty * 0.2)
    preference_penalty, preference_notes = _preference_penalty(text)
    freshness = assess_freshness(context, now)
    # Freshness brackets (§3.1): fresh posts get a boost, borderline-old posts
    # need real velocity, stale-and-slowing posts are an age-based WAIT.
    freshness_bonus = 0.0
    freshness_notes: list[str] = []
    if freshness:
        if freshness["bracket"] == "prime":
            freshness_bonus = 0.12
            freshness_notes.append(f"Fresh window: {freshness['note']}.")
        elif freshness["bracket"] == "needs_velocity" and not freshness["still_hot"]:
            freshness_bonus = -0.15
            freshness_notes.append("The post is aging and the conversation is not visibly accelerating.")
        elif freshness["bracket"] == "stale":
            pass  # handled below; the dying/room reads still apply first
        # The thread's own silence outranks any average: replies stopped
        # arriving, so a new one lands in an empty room whatever the bracket.
        if freshness.get("dying") and freshness["bracket"] in ("viable", "needs_velocity"):
            freshness_bonus -= 0.12
            quiet_for = _humanize_age(freshness["newest_reply_minutes"]).replace(" ago", "")
            freshness_notes.append(f"The thread went quiet {quiet_for} ago — the conversation moved on.")
        # Room size: a reply is read by whoever opens this post. A tiny room
        # makes even a good reply pointless; a huge fresh room is the exact
        # place replies work.
        if freshness.get("room") == "tiny" and freshness["bracket"] != "prime":
            freshness_bonus -= 0.10
            freshness_notes.append(
                f"This is a {_humanize_rate(freshness['views_per_hour'])} room — a reply here reaches almost nobody.")
        elif freshness.get("room") in ("big", "huge") and freshness["bracket"] in ("prime", "viable"):
            freshness_bonus += 0.05
            freshness_notes.append(
                f"A {_humanize_rate(freshness['views_per_hour'])} room in its reply window — this is where a reply gets seen.")
        # The widened reads: trajectory, crowding, first-mover.
        if freshness.get("trend") == "decelerating" and not freshness.get("dying"):
            freshness_bonus -= 0.06
            freshness_notes.append("Replies are thinning out — the conversation is past its peak.")
        if freshness.get("crowded"):
            freshness_bonus -= 0.08
            freshness_notes.append("The thread is already crowded — a late reply lands under the fold.")
        elif freshness.get("first_mover"):
            freshness_bonus += 0.06
            freshness_notes.append("Almost no replies yet inside the window — the first-mover slot is open.")
        if freshness.get("shape") == "conversation":
            freshness_bonus += 0.04
            freshness_notes.append("A conversation-dense thread — replies are what this audience reads.")
    # THE CO-SIGN READ. Reply engagement is mostly people AGREEING: a reply
    # that lands with the room gets liked by the room; a reply that argues
    # with the post gets, at best, a debate. The reply planner already knows
    # the stance deterministically — reuse it, never re-derive it here.
    try:
        from .reply_plan import read_the_room
        stance = read_the_room(context).move
    except Exception:                                       # noqa: BLE001
        stance = None
    window_open = not (freshness and (freshness.get("dying")
                                      or freshness["bracket"] == "stale"))
    if stance in ("agree_and_add", "answer", "add_detail", "joke") and window_open:
        freshness_bonus += 0.05
        freshness_notes.append(
            "The winning reply here lands WITH the room (" + stance.replace("_", " ")
            + ") — agreement is what gets co-signed and liked.")
    elif stance == "correct":
        freshness_bonus -= 0.06
        freshness_notes.append(
            "A reply here has to argue with the post — co-sign engagement is "
            "unlikely; only worth it to set a fact straight.")
    if freshness and freshness["bracket"] == "stale":
        if not freshness["still_hot"]:
            stale = _wait(
                "The reply window is closed",
                f"This was {_humanize_age(freshness['age_minutes']).replace(' ago', '')} old and is not accelerating — "
                "a late reply would be buried. Save the attention for a fresh conversation.",
                [freshness["note"].capitalize() + ".",
                 *([f"Average velocity is {_humanize_rate(freshness['views_per_hour'])} views — not enough to keep a late reply visible."]
                   if freshness.get("views_per_hour") is not None else [])],
                context,
            )
            stale["freshness"] = freshness
            return stale
        freshness_notes.append("The post is old but still accelerating — velocity beats age here.")
        freshness_bonus = 0.05
    reply_score = max(0.0, min(1.0, 0.22 + fit * 0.42 + conversation_signal * 0.26 + freshness_bonus - preference_penalty))
    # A quote needs a genuine frame and enough visible reach to justify pulling
    # the discussion onto the user's own profile. Otherwise a useful reply wins.
    distinct_frame_signal = 0.18 if context.quote_text or len(text) > 180 or stage == "active" else 0.0
    # Room shape steers the surface: a broadcast-shaped post (huge audience,
    # thin thread) favors a quote onto your own profile; a conversation-shaped
    # thread is exactly where a reply is read.
    if freshness and freshness.get("shape") == "broadcast":
        distinct_frame_signal += 0.06
    quote_score = max(0.0, min(1.0, 0.14 + fit * 0.34 + reach_signal * 0.22 + distinct_frame_signal + freshness_bonus * 0.5 - preference_penalty))

    if reply_score < 0.48 and quote_score < 0.52:
        result = _wait(
            "Skip this one",
            "The context does not create a strong enough opening for a useful reply or distinct quote. Saving attention for a better fit is the stronger strategy.",
            ["Topic fit is weak or unclear.", "A forced interaction would be more likely to look opportunistic than useful.", *freshness_notes, *preference_notes],
            context,
        )
        if freshness:
            result["freshness"] = freshness
        return result

    action = "reply" if reply_score >= quote_score else "quote"
    score = reply_score if action == "reply" else quote_score
    timing = timing_recommendations("conversation" if action == "reply" else "reach", profile.get("timezone", "UTC"), rows)
    reasons = [
        "The topic overlaps with the account’s current audience or content pillars." if topic_overlap else "The post offers a concrete point of entry rather than a generic trend.",
        "The visible conversation has signs of active discussion." if conversation_signal >= 0.2 else "A clear question or claim leaves room to add a distinct useful point.",
        *freshness_notes,
        *preference_notes,
    ]
    return {
        "action": action,
        "score": round(score, 3),
        "headline": "Reply now with the missing useful point" if action == "reply" else "Quote only if you can add a distinct frame",
        "rationale": "A reply is the shortest path into a relevant live conversation." if action == "reply" else "The original can travel farther only if your quote adds a viewpoint—not a restatement.",
        "reasons": reasons,
        "reaction_hypotheses": [
            "People following the conversation may respond if the reply makes the original idea more useful or precise.",
            "Your intended audience is more likely to notice a specific, distinct angle than a generic agreement.",
        ],
        "downside": "A generic agreement, recap, or promotional reply is likely to be ignored and can weaken audience trust.",
        "timing": timing,
        "confidence": "directional" if len(rows) >= 30 else "limited",
        "should_draft": True,
        "alternatives": ["Save the idea and wait for a stronger conversation", "Turn the distinct insight into a standalone post"],
        "conversation_stage": stage,
        "freshness": freshness,
    }


def _wait(headline: str, rationale: str, reasons: list[str], context: PostContext) -> dict[str, Any]:
    return {
        "action": "wait",
        "score": 0.0,
        "headline": headline,
        "rationale": rationale,
        "reasons": reasons,
        "reaction_hypotheses": ["Skipping preserves attention for a conversation where you can genuinely help."],
        "downside": "Forcing a response would risk irrelevant or repetitive engagement.",
        "timing": {"reason": "No response is recommended now."},
        "confidence": "strong",
        "should_draft": False,
        "alternatives": ["Save the topic", "Wait for a better opening"],
    }
