"""Deterministic account facts for chat: the model's ONLY source for numbers,
dates, and the user's own posts.

The live probes showed the chat model inventing the user's most recent post,
their posts-today count, a specific post's view count, and even today's date —
because none of that ever reached the prompt. This module computes all of it
straight from local storage (no model call) so the model can answer from facts
and is instructed to say "I don't have that" when a fact is absent.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Any

from .storage import get_account_profile, list_profile_snapshots, performance_records

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "what", "how", "many", "much", "did", "does",
    "was", "were", "get", "got", "post", "posts", "tweet", "tweets", "views", "view", "likes",
    "like", "my", "your", "about", "have", "has", "recent", "last", "best", "ever", "one",
}

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# A RATE ON A TINY DENOMINATOR IS NOT AN ESTIMATE. Live, 2026-08-02: he asked
# whether to delete a post that was seven minutes old with three views, and was
# told "it has a high engagement rate for you right now. Wait an hour." One like
# on three views is 33%. He pushed back — "but in 7 minutes and it onyl got 3
# vews is bad" — and he was right; it is the only place in his whole chat history
# where he catches the product out and the record proves him correct.
#
# CALIBRATED, not rounded. One extra interaction moves the rate by 100/N points:
#     3 views -> 33.3   ·   10 -> 10.0   ·   20 -> 5.0   ·   30 -> 3.3   ·   100 -> 1.0
# Below ~30 a single like swings the number by more than the differences he
# would ever act on. Measured against his own distribution (58 posts with view
# counts, median 310, p10 = 5): a floor of 30 means 31% of them report the
# interaction COUNT instead of a percentage. That is a large share and it is the
# right answer — those are the young and low-delivery posts where "3 views, 1
# interaction" is true and "33%" is not. 20 is defensible if 31% proves too
# blunt; below 20 the number is noise wearing a decimal point.
_RATE_MIN_VIEWS = 30


def _plain_views(row: dict[str, Any]) -> float | None:
    metrics = row.get("metrics") or {}
    value = metrics.get("views", metrics.get("impressions"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _snippet(row: dict[str, Any], limit: int = 300) -> str:
    """His own post, as he wrote it.

    The limit was 90 characters, which is a third of a tweet, and it cut on the
    raw index — so asking what his most recent post was returned it severed
    mid-word ("...instead of babysitti"). These are his own posts out of local
    storage; there is nothing to save by clipping them. 300 covers a standard
    post, and anything genuinely longer breaks at a word with an ellipsis so it
    reads as truncated instead of as a typo.
    """
    text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,.;:—-") + "…"


def _humanize_gap(minutes: float) -> str:
    """'3h ago' / '2 days ago'. Coarse on purpose — he wants the gap, not a clock."""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(round(minutes))} min ago"
    if minutes < 36 * 60:
        hours = minutes / 60.0
        return f"{hours:.0f}h ago" if hours >= 2 else "1h ago"
    days = minutes / (60.0 * 24.0)
    return f"{days:.0f} days ago" if days >= 2 else "yesterday"


def _post_line(row: dict[str, Any], tz_name: str = "UTC",
               now: datetime | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "created_at": row.get("created_at"),
        "type": row.get("post_type") or "post",
        "text": _snippet(row),
    }
    # Pre-rendered local weekday and clock. Handing the model a raw UTC ISO
    # string made it report the UTC calendar day, so a Sunday-evening post came
    # back as "Monday". The model should never have to convert a timezone.
    posted = _parse_dt(row.get("created_at"))
    if posted is not None:
        local = posted.astimezone(_zone(tz_name))
        line["posted_local"] = (f"{_DAY_NAMES[local.weekday()]} "
                                f"{local.strftime('%Y-%m-%d %H:%M')} ({tz_name})")
        # HOW LONG AGO, not just when. "What was my most recent post" is really
        # two questions, and the answer only ever carried one of them: he had a
        # calendar date and no sense of whether that was this morning or last
        # week. The model must never do this arithmetic itself.
        reference = now or datetime.now(timezone.utc)
        line["posted_ago"] = _humanize_gap(
            max(0.0, (reference - posted).total_seconds() / 60.0))
    metrics = row.get("metrics") or {}
    views = _plain_views(row)
    if views is not None:
        line["views"] = int(views)
    interactions = 0
    for key in ("replies", "likes", "reposts"):
        if metrics.get(key) is not None:
            line[key] = int(float(metrics[key]))
            interactions += line[key]
    # Views measure delivery; these measure whether anyone did anything about
    # it. Both were already stored per post and neither was ever summed, so
    # "how's my engagement" could only ever answer with views and a disclaimer
    # that per-post interactions were not available. They are.
    if any(key in line for key in ("replies", "likes", "reposts")):
        line["interactions"] = interactions
        if views and views >= _RATE_MIN_VIEWS:
            line["engagement_rate_pct"] = round(100.0 * interactions / views, 2)
    return line


def _message_tokens(message: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]{3,}", (message or "").lower())
            if token not in _STOPWORDS]


def _mentioned_posts(message: str, rows: list[dict[str, Any]], limit: int = 2,
                     tz_name: str = "UTC") -> list[dict[str, Any]]:
    """Posts the user is asking about by content ('my Kimi K3 post'). A match
    needs ≥2 message tokens in the post, or a single rare token (appears in
    ≤2 posts) — so 'Kimi' alone finds the Kimi post without false positives."""
    tokens = _message_tokens(message)
    if not tokens:
        return []
    post_token_sets = [(row, set(re.findall(r"[a-z0-9]{3,}", str(row.get("text") or "").lower())))
                       for row in rows]
    corpus_counts: Counter[str] = Counter()
    for _, token_set in post_token_sets:
        for token in set(tokens) & token_set:
            corpus_counts[token] += 1
    scored = []
    for row, token_set in post_token_sets:
        hits = [token for token in tokens if token in token_set]
        if not hits:
            continue
        rare_hit = any(corpus_counts[token] <= 2 for token in hits)
        if len(set(hits)) >= 2 or rare_hit:
            scored.append((len(set(hits)) + (1 if rare_hit else 0), row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_post_line(row, tz_name) for _, row in scored[:limit]]


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _zone(name: str):
    """Resolve a zone NAME, falling back to the customer's own zone.

    Windows reports local zones as display names ("Eastern Daylight Time"), which ZoneInfo cannot
    parse. Returning UTC on that failure silently relabelled UTC clock times as the customer's zone
    - worse than the original bug, because the label then asserted something false.

    What "the customer's zone" is depends on where this runs, and getting that wrong is how the
    original bug came back. A hosted engine's machine clock is UTC and says nothing about the
    customer, so the fallback is the zone the browser reported for this request; only a development
    run may ask the machine. `posting_hours.effective_timezone` owns that decision.
    """
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    from .posting_hours import effective_timezone
    requested = effective_timezone()
    # "UTC" from there is the schema's untouched default, not a statement about the customer
    # (`_display_timezone` reads it the same way), so it is no better than the name that just failed.
    if requested and requested.upper() != "UTC" and requested != name:
        try:
            return ZoneInfo(requested)
        except Exception:                                   # noqa: BLE001
            pass
    from .security import hosted
    if hosted():
        return timezone.utc          # nothing better is known; never the server's own clock label
    return datetime.now().astimezone().tzinfo or timezone.utc


def _display_timezone(profile: dict[str, Any]) -> str:
    """The zone the customer actually lives in, not UTC.

    Every time in chat used to default to UTC, because the profile field is normally unset. That
    reported "you posted ~19:24 UTC on Monday" for a post made on SUNDAY evening — the UTC calendar
    day had already rolled over. The clock and the weekday were both wrong from where the customer
    sits, and they had to do the conversion in their head to check anything.

    An explicit profile setting always wins. Otherwise the answer is the zone the browser reported
    for this request: a hosted engine runs on a server whose clock is UTC, so asking the machine
    (which is what this did when the engine was a desktop app) reinstates exactly the bug above for
    every customer who has not set a zone. A development run still falls back to the machine.
    """
    configured = str(profile.get("timezone") or "").strip()
    if configured and configured.upper() != "UTC":
        return configured
    from .posting_hours import effective_timezone
    requested = effective_timezone()
    if requested and requested.upper() != "UTC":
        return requested
    from .security import hosted
    if hosted():
        return "UTC"                 # honest: nothing here knows where the customer is
    local = datetime.now().astimezone().tzinfo
    name = getattr(local, "key", None) or (local.tzname(datetime.now()) if local else None)
    return str(name) if name else "UTC"


def _period_totals(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Views and post counts over the last 7 and 30 days.

    Asked "how many views did I get this week", the model answered "4,886 views
    this week — that's your best post ever": it reached for the nearest number
    in the facts and relabelled it. Asked "what's my reach" it reported a
    "median reach score" of 576, which is not a metric this system has and not
    a number that appears anywhere in its input.

    Both are the same failure and it is not a prompting problem — the period
    aggregate genuinely was not in the facts, so there was nothing to answer
    from. Every window here is summed from the same rows the medians use, and
    `*_measured` says how many posts actually carried a view count, so a
    partial window is never reported as a total.
    """
    windows = {7: "7", 30: "30"}
    out: dict[str, Any] = {}
    for days, label in windows.items():
        cutoff = now - timedelta(days=days)
        views, measured, posts = 0, 0, 0
        for row in rows:
            created = _parse_dt(row.get("created_at"))
            if created is None or created < cutoff:
                continue
            posts += 1
            value = _plain_views(row)
            if value is not None:
                views += int(value)
                measured += 1
        out[f"posts_last_{label}_days"] = posts
        out[f"views_last_{label}_days"] = views if measured else None
        out[f"posts_measured_last_{label}_days"] = measured
    return out


def _habit_facts(rows: list[dict[str, Any]], tz_name: str,
                 now: datetime) -> dict[str, Any]:
    """What he ACTUALLY does: how often he posts, when, and his best day.

    Two more numbers the model reached for and could not find. Asked "am I
    posting too much" it answered "you're posting 3 times a day, which is
    exactly your daily budget" — 3 is the BUDGET; he had posted nothing that
    day. Asked "what time do I usually post" it answered with when he SHOULD
    post, and said there was not enough history to know, with 36 posts on file.

    Both are descriptions of his own history, which is entirely local. The
    budget is a recommendation and the rate is a measurement, and nothing here
    may let one be reported as the other.
    """
    zone = _zone(tz_name)
    hours: Counter[int] = Counter()
    by_day: dict[Any, int] = {}
    recent_posts = 0
    cutoff = now - timedelta(days=30)
    for row in rows:
        created = _parse_dt(row.get("created_at"))
        if created is None:
            continue
        local = created.astimezone(zone)
        hours[local.hour] += 1
        views = _plain_views(row)
        if views is not None:
            by_day[local.date()] = by_day.get(local.date(), 0) + int(views)
        if created >= cutoff:
            recent_posts += 1
    out: dict[str, Any] = {}
    if recent_posts:
        # Over the window he was actually active in, not a flat 30: a week-old
        # account divided by 30 reports a rate a fifth of the truth.
        oldest = min((_parse_dt(row.get("created_at")) for row in rows
                      if _parse_dt(row.get("created_at")) is not None
                      and _parse_dt(row.get("created_at")) >= cutoff),
                     default=None)
        span_days = max(1.0, (now - oldest).total_seconds() / 86400.0) if oldest else 30.0
        out["posts_per_day_measured"] = round(recent_posts / span_days, 1)
        out["rate_window_days"] = round(span_days, 1)
    if hours:
        out["usual_hours_local"] = [
            {"hour": f"{hour:02d}:00", "posts": count}
            for hour, count in hours.most_common(3)]
    if by_day:
        day, total = max(by_day.items(), key=lambda item: item[1])
        out["best_day_by_views"] = {
            "date": day.isoformat(),
            "weekday": _DAY_NAMES[day.weekday()],
            "views": total,
            "posts": sum(1 for row in rows
                         if (parsed := _parse_dt(row.get("created_at"))) is not None
                         and parsed.astimezone(zone).date() == day),
        }
    return out


def _interaction_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Likes, replies and reposts across the account — the numbers chat used to
    say it did not have.

    "I don't have per-post likes and replies aggregated, so I won't guess
    those" was true of the aggregation and not of the data: every one of them
    is already on the row that the view count is read from. Refusing to sum
    them meant every engagement question could only ever be answered with
    views, which measure delivery and not whether anyone did anything.
    """
    totals = {"likes": 0, "replies": 0, "reposts": 0}
    rates: list[float] = []
    counted = 0
    for row in rows:
        metrics = row.get("metrics") or {}
        present = False
        for key in totals:
            value = metrics.get(key)
            if value is not None:
                try:
                    totals[key] += int(float(value))
                    present = True
                except (TypeError, ValueError):
                    continue
        if not present:
            continue
        counted += 1
        views = _plain_views(row)
        # SAME FLOOR ON THE MEDIAN. A three-view post contributing 33% does not
        # just mislead about itself — it drags the account-wide median, which is
        # the number that answers "how is my engagement". The totals below are
        # unaffected: a like is a like whatever the denominator.
        if views and views >= _RATE_MIN_VIEWS:
            interactions = sum(int(float(metrics[k])) for k in totals
                               if metrics.get(k) is not None)
            rates.append(100.0 * interactions / views)
    if not counted:
        return {"posts_with_interactions": 0}
    return {
        "posts_with_interactions": counted,
        "total_likes": totals["likes"],
        "total_replies": totals["replies"],
        "total_reposts": totals["reposts"],
        "median_engagement_rate_pct": round(median(rates), 2) if rates else None,
    }


def _follower_trend() -> dict[str, Any] | None:
    """Followers now vs the oldest snapshot on record, or None.

    A follower count on its own answers "how many" and nothing else. The
    snapshots to answer "and is that going up" were already being written; only
    the latest one was ever read. Returns None rather than a zero when there is
    only one snapshot — no second reading is not the same claim as no change.
    """
    try:
        snapshots = [snap for snap in list_profile_snapshots(limit=500)
                     if snap.get("followers_count") is not None]
    except Exception:                                        # noqa: BLE001
        return None
    if len(snapshots) < 2:
        return None
    snapshots.sort(key=lambda snap: str(snap.get("captured_at") or ""))
    first, last = snapshots[0], snapshots[-1]
    start, end = _parse_dt(first.get("captured_at")), _parse_dt(last.get("captured_at"))
    if start is None or end is None or end <= start:
        return None
    delta = int(last["followers_count"]) - int(first["followers_count"])
    days = max(1.0, (end - start).total_seconds() / 86400.0)
    return {
        "change": delta,
        "over_days": round(days, 1),
        "from": int(first["followers_count"]),
        "to": int(last["followers_count"]),
        "per_week": round(delta / days * 7.0, 1),
        "snapshots": len(snapshots),
    }


def _format_medians() -> dict[str, Any]:
    """Reply vs post vs quote, each with the sample behind it.

    THE ONE AGGREGATE THAT WAS MISSING WHEN HE ASKED THE BEST QUESTION IN HIS
    CORPUS. "explain why my replies do better than my posts" (2026-08-04) was
    routed to the drafting path because the word "replies" claimed it, and after
    that was fixed it fell through to the model — which had `median_views` (one
    global number) and no reply-vs-post split at all. The comparison he asked
    about did not exist in the facts, so the only way to answer was to invent
    it. That is the standing lesson of this file, one field over: *a missing
    aggregate becomes a fabricated one.*

    `performance_insights().by_format` has computed this the whole time;
    `account_review` and the `whats_working` capability both read it. Only the
    chat facts never did.

    EVERY MEDIAN CARRIES ITS SAMPLE, for the reason `_diagnose_performance`
    learned the hard way: "your replies do better — a median of 588 views" was
    stated off SIX replies as a strategy recommendation. On his live data today
    the reply median is 488 across 7 measured against a post median of 226
    across 55 — a real difference and a very thin sample, and the model cannot
    say the second part unless it is handed it.
    """
    try:
        from .performance import performance_insights
        insights = performance_insights()
    except Exception:                                        # noqa: BLE001
        return {}
    by_format = insights.get("by_format") or {}
    out: dict[str, Any] = {}
    for name in ("post", "reply", "quote"):
        row = by_format.get(name) or {}
        if row.get("median_views") is None:
            continue
        out[name] = {
            "median_views": int(row["median_views"]),
            "measured": int(row.get("measured_views") or row.get("posts") or 0),
            "evidence": str(row.get("evidence") or "limited"),
        }
    if not out:
        return {}
    return {
        "median_views_by_format": out,
        "strongest_format": insights.get("strongest_format"),
        "format_stat_note": (
            "median_views_by_format is the ONLY basis for comparing replies, "
            "posts and quotes to each other. Always state the `measured` count "
            "beside any median you quote from it, and say the sample is thin "
            "when `evidence` is not 'solid' - a format can look twice as strong "
            "off seven measured items and mean nothing. Never compare a format "
            "that is absent here: it has no measured views, which is not the "
            "same claim as zero."),
    }


def account_facts(message: str = "") -> dict[str, Any]:
    """Everything the chat model may state as fact about this account."""
    profile = get_account_profile()
    timezone_name = _display_timezone(profile)
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(_zone(timezone_name))
    rows = performance_records()
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    dated_today = 0
    for row in rows:
        if str(row.get("post_type") or "post") not in ("post", "original"):
            continue
        created = str(row.get("created_at") or "")
        # Local day, not the UTC one: after ~19:00 local (US zones) the UTC
        # date has already rolled over, so an evening post counted toward
        # tomorrow and "posts today" read 0 right after posting.
        created_dt = _parse_dt(created)
        if created_dt is not None and created_dt.astimezone(_zone(timezone_name)).date() == local_now.date():
            dated_today += 1
    measured_views = [value for row in rows if (value := _plain_views(row)) is not None]
    best_row = max((row for row in rows if _plain_views(row) is not None),
                   key=lambda row: _plain_views(row) or 0.0, default=None)
    followers = None
    try:
        snapshots = list_profile_snapshots(limit=1)
        if snapshots and snapshots[0].get("followers_count") is not None:
            followers = int(snapshots[0]["followers_count"])
    except Exception:
        followers = None
    try:
        from .performance import daily_post_budget, timing_recommendations
        budget, budget_evidence = daily_post_budget(rows, timezone_name)
        windows = (timing_recommendations("reach", timezone_name, rows).get("windows") or [])[:2]
    except Exception:
        budget, budget_evidence, windows = 3, None, []
    niche = None
    try:
        from .account_model import infer_account_model, personal_topic_profile
        account = infer_account_model(rows)
        pillars = [str(p) for p in (account.get("content_pillars") or [])][:4]
        raw_themes = [str(cluster.get("label", "")).strip()
                      for cluster in personal_topic_profile(rows).get("clusters", [])[:4]]
        # Cluster labels are sometimes raw token bags ("gpt-5 / has / chatgpt/
        # fable/back") that leaked verbatim into answers. Only keep ones that
        # read like real topics.
        themes = [t for t in raw_themes if t and "/" not in t and len(t.split()) <= 4][:3]
        if pillars:
            niche = "This account posts about: " + ", ".join(pillars) + "."
            if themes:
                niche += " Recurring themes: " + ", ".join(themes) + "."
    except Exception:
        niche = None
    facts: dict[str, Any] = {
        "now": (f"{_DAY_NAMES[local_now.weekday()]} {local_now.date().isoformat()} "
                f"{local_now.strftime('%H:%M')} ({timezone_name}, the user's local time)"),
        "timezone_note": (f"All days and hours below are the user's LOCAL time ({timezone_name}). "
                          "State them exactly as given and never convert them to UTC or "
                          "append a UTC label - the user does not think in UTC."),
        "followers": followers,
        "posts_today": dated_today,
        "daily_post_budget": budget,
        "budget_evidence": budget_evidence,
        "total_posts_recorded": len(rows),
        "posts_with_measured_views": len(measured_views),
        "median_views": int(median(measured_views)) if measured_views else None,
        # THE MEAN AND THE TOTAL, because they were absent and asked for.
        #
        # "a missing aggregate becomes a fabricated one" is the standing lesson in
        # this file — see the `median reach score` of 576 above, a metric this
        # system does not have, invented because the number asked for was not
        # here to give. The same hole was open for the two most ordinary
        # performance questions there are.
        #
        # Measured on his live archive 2026-08-05, 56 posts with views:
        #   median 310 · MEAN 872 · max 17,787
        # Asked "what's my average views", the only labelled number available was
        # the median, and substituting it understates the mean by 562 views — 64%.
        # Asked "how many views in total", the largest labelled number was
        # views_last_30_days at 45,236 against a true 48,841.
        #
        # Both are stated, neither replaces the median: every comparison this
        # product makes ("52 views against your median post of 298") stays on the
        # median on purpose, because one 17,787-view post drags a mean away from
        # anything a typical post can be judged against. The note says so, so the
        # model reports the mean when asked for it and judges on the median.
        "mean_views": int(fmean(measured_views)) if measured_views else None,
        "total_views_measured": int(sum(measured_views)) if measured_views else None,
        "views_stat_note": (
            "median_views is the yardstick for whether a post did well - one "
            "outlier drags the mean far from a typical post. Report mean_views "
            "only when the user asks for an average or mean, and "
            "total_views_measured only when they ask for a total; never relabel "
            "one as the other, and never present a 7-day or 30-day figure as an "
            "all-time total."),
        **_format_medians(),
        **_interaction_totals(rows),
        **_period_totals(rows, now),
        **_habit_facts(rows, timezone_name, now),
        "recent_posts": [_post_line(row, timezone_name, now) for row in rows[:5]],
        # THE TIMEZONE ARGUMENT WAS MISSING HERE and nowhere else, so his best
        # post — the one fact he is most likely to repeat out loud — came back
        # as "Wednesday 2026-07-22 23:09 (UTC)": the UTC clock, wearing a label
        # that says it is his. It was 19:09 his time. Exactly the bug this
        # module's docstring says was fixed, surviving in the one call that
        # forgot to pass the zone.
        "best_post_ever": _post_line(best_row, timezone_name, now) if best_row else None,
        "niche": niche,
        "best_windows": windows,
        "follower_trend": _follower_trend(),
    }
    mentioned = _mentioned_posts(message, rows, tz_name=timezone_name)
    if mentioned:
        facts["mentioned_posts"] = mentioned
    return facts
