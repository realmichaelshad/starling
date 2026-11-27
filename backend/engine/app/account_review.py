"""An honest read of the account, and what to do about it.

Asked "what do you think of my account honestly, what should I do to make it
better", Starling had no handler and fell through to the model — the same path
that answered "You're not growing" over a payload showing +5 followers, and
"I'm not slow" after taking 88.9 seconds to say it. A judgement question invites
a judgement, and the standing instruction to trust `account_facts` loses to that
pressure every time.

Nothing here computes anything new. `performance_insights()` already knows his
strongest format and the median views behind it; `account_facts()` already knows
the ceiling, the trend and the cadence; `timing_recommendations()` already has a
real finding about his hours; `next_moves()` already ranks concrete moves. The
analysis existed and nothing called it for this question.

Two rules hold every line below:

  * EVERY CLAIM CARRIES ITS NUMBER AND ITS SAMPLE SIZE. "Replies earn more" is
    an opinion; "replies median 488 across 4 against 302 across 37" is a finding
    he can check, and the sample size is what stops a 4-post median being read
    as a law.
  * NO EXTERNAL BENCHMARK. Starling can see exactly one account. Any verdict on
    where he ranks against others would be invented, and inventing one is
    precisely how "you're not growing" got said.
"""
from __future__ import annotations

from typing import Any


def _fmt(value: Any) -> str:
    """350.0 -> '350'. by_format medians are floats and read as fake precision."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{int(round(number)):,}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _clip(text: str, limit: int) -> str:
    """Cut at a word boundary. A hard slice ended a line on "manufactur"."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or text[:limit]) + "…"


def _ordinal(number: int) -> str:
    """61 -> '61st'. Naive suffixing produced "the 61th percentile"."""
    if 10 <= number % 100 <= 20:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }".replace(" ", "")


def _format_gap(insights: dict) -> dict | None:
    """The format that earns more than the one he actually uses.

    His headline finding: replies median 488 across 4, standalone posts 302
    across 37. The opportunity is not "post better", it is "the thing that
    works is the thing you have barely tried".
    """
    by_format = insights.get("by_format") or {}
    posts = by_format.get("post") or {}
    replies = by_format.get("reply") or {}
    if not posts.get("posts") or not replies.get("posts"):
        return None
    post_median = posts.get("median_views")
    reply_median = replies.get("median_views")
    if post_median is None or reply_median is None:
        return None
    if float(reply_median) <= float(post_median):
        return None
    # Under-used is what makes it a gap rather than a fact. This one stays on
    # `posts` deliberately: "you barely send any" is about volume SENT, not about
    # how many of them carried a view count.
    if int(replies["posts"]) >= int(posts["posts"]):
        return None
    # THE SAMPLE BEHIND THE MEDIAN, NOT THE ROW COUNT.
    #
    # This said "across {posts['posts']} standalone posts" — every row of that
    # format — while `median_views` is the median of only the rows that actually
    # carried a view count. On his archive that overstated the evidence by 78%
    # (95 rows against 56 measured), and the sentence's whole job is to say how
    # much to trust the number it just quoted. `performance_insights` returns
    # `measured_views` precisely so this cannot happen; nothing was reading it.
    reply_n = int(replies.get("measured_views") or replies["posts"] or 0)
    post_n = int(posts.get("measured_views") or posts["posts"] or 0)
    return {
        "kind": "format_gap",
        "weight": 100,
        "text": (f"Your replies out-earn your posts and you barely send any. "
                 f"Median {_fmt(reply_median)} views across "
                 f"{reply_n} {_plural(reply_n, 'reply', 'replies')}, "
                 f"against {_fmt(post_median)} across {post_n} standalone "
                 f"{_plural(post_n, 'post')}. "
                 f"That is the gap — {replies.get('evidence', 'limited')} evidence "
                 f"on the reply side, so treat it as a lead worth testing rather "
                 f"than a law."),
    }


def _ceiling_gap(facts: dict) -> dict | None:
    """A high best against a low median: reach is possible, not repeatable."""
    best = facts.get("best_post_ever") or {}
    ceiling = best.get("views")
    median = facts.get("median_views")
    if ceiling is None or not median:
        return None
    if float(ceiling) < float(median) * 3:
        return None
    kind = {"reply": "reply", "quote": "quote-tweet"}.get(
        str(best.get("type") or "post"), "post")
    return {
        "kind": "ceiling",
        "weight": 80,
        "text": (f"You can reach, you just can't repeat it. One {kind} hit "
                 f"{_fmt(ceiling)} views while your median is {_fmt(median)} — "
                 f"so distribution is not the wall, consistency is."),
    }


def _compounding(facts: dict) -> dict | None:
    """Followers per week against how much he is publishing to get them."""
    trend = facts.get("follower_trend") or {}
    change, days = trend.get("change"), trend.get("over_days")
    if change is None or not days:
        return None
    per_week = trend.get("per_week")
    per_day = facts.get("posts_per_day_measured")
    body = (f"Followers are up {change} over {days:g} days"
            if change > 0 else
            f"Followers are down {abs(change)} over {days:g} days"
            if change < 0 else
            f"Followers are flat over {days:g} days")
    if per_week:
        body += f" (~{per_week:g}/week)"
    if per_day:
        body += f", at about {per_day:g} posts a day"
    return {"kind": "compounding", "weight": 60, "text": body + "."}


def _timing(insights: dict, timing: dict | None) -> dict | None:
    """A real window, or the finding that there isn't one. Never a guess."""
    windows = insights.get("best_windows") or []
    if windows:
        first = windows[0]
        return {
            "kind": "timing",
            "weight": 40,
            "text": (f"Your strongest measured window is {first.get('day')} around "
                     f"{first.get('start_hour', 0):02d}:00–"
                     f"{first.get('end_hour', 0):02d}:00. A correlation across "
                     f"{insights.get('timing_sample_size', 0)} posts, so test it "
                     f"rather than trusting it."),
        }
    sample = int((timing or {}).get("sample_size") or
                 insights.get("timing_sample_size") or 0)
    if sample < 20:
        return None
    return {
        "kind": "timing",
        "weight": 40,
        "text": (f"Timing is not your problem. No hour reliably beats your own "
                 f"average across {sample} dated posts, so stop optimising when "
                 f"you post and put the effort into what you post."),
    }


def _topic(insights: dict) -> dict | None:
    themes = [t for t in (insights.get("topic_themes") or [])
              if isinstance(t, dict) and t.get("label")]
    if not themes:
        return None
    best = max(themes, key=lambda t: float(t.get("median_percentile") or 0))
    percentile = best.get("median_percentile")
    if percentile is None:
        return None
    # `measured`, not `size` — same rule as `_format_gap`. `median_percentile` is
    # the median of the cluster members that carried a view count, while `size` is
    # every member. Quoting `size` as the sample behind the percentile inflated the
    # evidence for the one sentence whose job is to say how much of it there is.
    # `performance.py` already puts `measured` in every theme dict.
    size = int(best.get("measured") or best.get("size") or 0)
    return {
        # Above the follower trend: a theme he can act on beats a restatement
        # of the trend line, and only three findings are shown.
        "kind": "topic",
        "weight": 65,
        "text": (f"Your strongest theme is “{best['label']}” — it sits at the "
                 f"{_ordinal(int(round(float(percentile) * 100)))} percentile of "
                 f"your own results across {size} {_plural(size, 'post')} "
                 f"({best.get('evidence', 'limited')} evidence)."),
    }


def _cadence(facts: dict) -> dict | None:
    """Publishing far under the budget is a real, actionable finding."""
    per_day = facts.get("posts_per_day_measured")
    budget = facts.get("daily_post_budget")
    if not per_day or not budget:
        return None
    if float(per_day) >= float(budget) * 0.6:
        return None
    return {
        "kind": "cadence",
        "weight": 30,
        "text": (f"You're averaging {per_day:g} posts a day against a budget of "
                 f"{budget}. There is room to publish more without crowding "
                 f"yourself."),
    }


def account_review() -> dict[str, Any]:
    """A blunt, grounded read plus ranked moves. Synthesis only."""
    from .account_facts import account_facts
    from .performance import performance_insights, timing_recommendations
    from .storage import get_account_profile, performance_records

    try:
        facts = account_facts("")
    except Exception:                                        # noqa: BLE001
        facts = {}
    try:
        insights = performance_insights()
    except Exception:                                        # noqa: BLE001
        insights = {}
    timing: dict | None = None
    try:
        timing = timing_recommendations(
            "reach", get_account_profile().get("timezone", "UTC"),
            performance_records())
    except Exception:                                        # noqa: BLE001
        timing = None

    followers = facts.get("followers")
    total = facts.get("total_posts_recorded")
    measured = facts.get("posts_with_measured_views")
    if not total and not followers:
        return {
            "headline": ("I don't have enough captured history to read your "
                         "account yet. Run a sync and ask me again."),
            "diagnosis": [], "moves": [], "evidence": {},
        }

    findings = [entry for entry in (
        _format_gap(insights),
        _ceiling_gap(facts),
        _topic(insights),
        _compounding(facts),
        _timing(insights, timing),
        _cadence(facts),
    ) if entry]
    findings.sort(key=lambda entry: -entry["weight"])

    scale = []
    if followers is not None:
        scale.append(f"{followers} {_plural(int(followers), 'follower')}")
    if total:
        scale.append(f"{total} {_plural(int(total), 'post')} on record")
    if facts.get("median_views"):
        scale.append(f"a median of {_fmt(facts['median_views'])} views"
                     + (f" across {measured} measured" if measured else ""))
    headline = ("Honest read: " + ", ".join(scale) + "."
                if scale else "Honest read of what I have:")

    moves: list[str] = []
    try:
        from .planner import next_moves
        plan = next_moves(llm=None, limit=3)
        for move in (plan.get("moves") or [])[:3]:
            # The VALUE of a reply move is its target, never the word "reply".
            action = str(move.get("move") or "post").strip()
            topic = str(move.get("topic") or move.get("target_hint") or "").strip()
            # A wait move carries no topic, so "{move}: {topic}" printed
            # "wait: wait". Name the action once when there is nothing to name.
            head = f"{action}: {topic}" if topic and topic != action else action
            why = " ".join(str(move.get("why") or "").split())
            moves.append(head + (f" — {_clip(why, 120)}" if why else ""))
    except Exception:                                        # noqa: BLE001
        moves = []
    if not moves and findings:
        # Never leave him with a diagnosis and no action. The top finding always
        # implies one, so derive it rather than printing nothing.
        top = findings[0]
        moves = [{
            "format_gap": "Send replies under bigger accounts in your lane — "
                          "your own numbers say they out-earn your posts.",
            "ceiling": "Work out what the one that hit had that the median "
                       "ones don't, and repeat that, not the topic.",
            "topic": "Post more inside your strongest theme while it is live.",
            "timing": "Stop optimising your posting hour; put the effort into "
                      "the post itself.",
            "cadence": "Publish more — you are well under your own budget.",
            "compounding": "Pick one format and one theme and hold both for a "
                           "fortnight, so the numbers mean something.",
        }.get(top["kind"], "Pick one format and one theme and hold both.")]

    return {
        "headline": headline,
        "diagnosis": [entry["text"] for entry in findings],
        "moves": moves,
        "evidence": {
            "followers": followers,
            "total_posts": total,
            "measured_posts": measured,
            "median_views": facts.get("median_views"),
            "strongest_format": insights.get("strongest_format"),
            "timing_sample_size": (timing or {}).get("sample_size"),
        },
    }


def account_review_text() -> str:
    """The review as prose, blunt first. Roast mode is a separate, harsher read."""
    review = account_review()
    if not review["diagnosis"] and not review["moves"]:
        return review["headline"]
    parts = [review["headline"]]
    parts.extend(review["diagnosis"][:3])
    if review["moves"]:
        parts.append("What I'd do, strongest first:")
        parts.extend(f"{index}. {move}"
                     for index, move in enumerate(review["moves"][:3], start=1))
    # The limit is a FINDING, not a disclaimer to bury. Without it a reader
    # assumes the verdict is against other accounts, which is the assumption
    # that produced "you're not growing".
    parts.append("All of that is against your own history — I can see one "
                 "account, so I won't tell you how it compares to anyone else's.")
    return "\n".join(parts)
