"""Account questions must be answered from measurements, or not at all.

Found by chatting with a live companion against his real database. Every one of
these was answered confidently and wrongly, and each has the same shape: the
aggregate needed to answer was not in `account_facts`, so the model reached for
the nearest number in the payload and relabelled it.

    "how many views did I get this week"  -> "4,886 views this week. That's
                                             your best post ever."  (a POST)
    "what's my reach"                     -> "your median reach score is 576"
                                             (no such metric; no such number)
    "am I posting too much"               -> "you're posting 3 times a day,
                                             exactly your daily budget" (0 today;
                                             3 is the BUDGET, not the rate)

The other three were intent collisions — the right data routed to the wrong
question. "when was my last post" was answered with when he SHOULD post,
"what's my worst post" with his most RECENT one, and "how many replies have I
sent" with the drafting prompt, because the word "reply" reads as an
instruction to write one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage import set_account_profile, upsert_post
from app.strategy import _facts_question_answer


def _seed() -> None:
    """Five posts with known numbers. Totals: 5,884 views, 2 replies."""
    set_account_profile({"audience": "AI builders",
                         "content_pillars": ["AI models", "agents"],
                         "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    seeded = [
        ("p1", "post", 0.5, 4886, 3),
        ("p2", "post", 1.5, 300, 1),
        ("p3", "post", 2.5, 12, 0),
        ("p4", "reply", 3.5, 486, 2),
        ("p5", "reply", 4.5, 200, 0),
    ]
    for post_id, kind, days_ago, views, likes in seeded:
        created = now - timedelta(days=days_ago)
        upsert_post({"post_id": post_id, "post_type": kind,
                     "created_at": created.isoformat(),
                     "text": f"seeded {post_id}, a body long enough to read back",
                     "metrics": {"views": views, "likes": likes, "replies": 0},
                     "metrics_updated_at": now.isoformat()})


def test_weekly_views_are_summed_not_borrowed_from_the_best_post() -> None:
    _seed()
    reply = _facts_question_answer("how many views did i get this week")
    assert reply is not None
    assert "5,884" in reply, reply
    assert "4,886" not in reply and "4886" not in reply


def test_reach_names_what_the_number_is_and_invents_no_score() -> None:
    _seed()
    reply = _facts_question_answer("whats my reach")
    assert reply is not None
    assert "views" in reply.lower()
    assert "no score" in reply.lower(), f"invented a score: {reply}"


def test_cadence_reports_the_measured_rate_not_the_budget() -> None:
    _seed()
    reply = _facts_question_answer("am i posting too much")
    assert reply is not None
    assert "averaging" in reply.lower(), reply
    assert "you're posting 3 times a day" not in reply.lower()


def test_when_was_my_last_post_is_a_fact_not_advice() -> None:
    _seed()
    reply = _facts_question_answer("when was my last post")
    assert reply is not None
    assert "went out" in reply, reply
    assert "best time" not in reply.lower()


def test_worst_post_is_the_worst_one_not_the_latest() -> None:
    _seed()
    reply = _facts_question_answer("what is my worst post")
    assert reply is not None
    assert "12 views" in reply, f"did not rank by views: {reply}"


def test_reply_count_is_counted_not_drafted() -> None:
    _seed()
    reply = _facts_question_answer("how many replies have i sent")
    assert reply is not None
    assert "2 replies" in reply, reply
    assert "paperclip" not in reply.lower()


def test_usual_posting_time_describes_history_not_advice() -> None:
    _seed()
    reply = _facts_question_answer("what time do i usually post")
    assert reply is not None
    assert "mostly post around" in reply.lower(), reply
    assert "don't have enough posting history" not in reply.lower()


def test_asking_when_to_post_still_gets_advice() -> None:
    """The guards above must not swallow the question they were carved out of."""
    _seed()
    reply = _facts_question_answer("when should i post")
    assert reply is not None
    assert "mostly post around" not in reply.lower()
    assert "went out" not in reply


def test_a_recorded_zero_is_an_answer() -> None:
    """"How many likes did my last post get" came back with the VIEW count.

    The likes were on the row and equal to zero, which the formatter dropped as
    falsy — so the one number he asked for was the one number omitted.
    """
    _seed()
    reply = _facts_question_answer("how many likes did my last post get")
    assert reply is not None
    assert "3 likes" in reply, reply


def test_ranking_his_own_posts_is_not_an_unverifiable_comparison() -> None:
    """It answered "I won't rank models I can't verify" — about his own posts.

    The guard is for "is GPT-5 better than Claude". It matches on "which …
    best", which is also how he asks about local data.
    """
    from app.main import _out_of_scope_answer
    assert _out_of_scope_answer("is gpt-5 better than claude") is not None
    assert _out_of_scope_answer("which of my posts did best this month") is None
    assert _out_of_scope_answer("which of my tweets got the most views") is None


def test_best_in_a_window_is_ranked_from_storage() -> None:
    _seed()
    reply = _facts_question_answer("which of my posts did best this month")
    assert reply is not None
    assert "4886" in reply, reply
    assert "rank models" not in reply


def test_no_best_window_is_a_finding_not_an_absence_of_data() -> None:
    """"When should I post" claimed no history, with 36 dated posts on file.

    The timing engine had already produced the real answer — no hour reliably
    beats his own average — and this branch replaced it with a false disclaimer.
    """
    set_account_profile({"audience": "AI builders",
                         "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for index in range(36):
        created = now - timedelta(days=index % 20, hours=index % 24)
        upsert_post({"post_id": f"t{index}", "post_type": "post",
                     "created_at": created.isoformat(),
                     "text": f"dated post {index} with enough body to count",
                     "metrics": {"views": 400 + (index % 7) * 10},
                     "metrics_updated_at": now.isoformat()})
    reply = _facts_question_answer("when should i post")
    assert reply is not None
    assert "don't have enough posting history" not in reply.lower(), reply
    assert "36 dated posts" in reply, reply


def test_is_that_good_never_contradicts_its_own_numbers() -> None:
    """It answered "You're not growing" over a payload showing +5 in 5 days.

    A judgement question makes the model reach for a judgement; the record is
    right there, and there is no other account to rank him against.
    """
    from app.storage import save_profile_snapshot
    _seed()
    now = datetime.now(timezone.utc)
    save_profile_snapshot({"captured_at": (now - timedelta(days=5)).isoformat(),
                           "followers_count": 17})
    save_profile_snapshot({"captured_at": now.isoformat(), "followers_count": 22})
    reply = _facts_question_answer("is that good")
    assert reply is not None
    assert "up 5" in reply, reply
    assert "not growing" not in reply.lower()
    # No invented benchmark against other accounts.
    assert "no other accounts" in reply.lower()


@pytest.mark.parametrize("message", [
    "is that good enough for a post",
    "is that a good idea for a thread",
    "what should i post",
])
def test_the_verdict_answer_only_takes_the_bare_follow_up(message: str) -> None:
    _seed()
    reply = _facts_question_answer(message)
    assert reply is None or "no other accounts" not in reply.lower()


def test_best_post_clock_is_the_users_zone() -> None:
    """It was printed as the UTC clock wearing the user's zone label."""
    set_account_profile({"audience": "AI builders",
                         "content_pillars": ["AI"], "timezone": "America/New_York"})
    upsert_post({"post_id": "b1", "post_type": "post",
                 "created_at": "2026-07-22T23:09:34+00:00",
                 "text": "the best one, by a distance",
                 "metrics": {"views": 4886}, "metrics_updated_at": "2026-07-23T00:00:00+00:00"})
    reply = _facts_question_answer("whats my best post")
    assert reply is not None
    assert "19:09" in reply, reply
    assert "23:09" not in reply


# --- a period in the question is part of the question ----------------------- #

def _seed_across_months() -> None:
    """Six posts in one calendar month, two in the next."""
    set_account_profile({"audience": "AI builders", "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    old_month = (now.replace(day=1) - timedelta(days=5)).replace(day=10)
    for index in range(6):
        created = old_month + timedelta(hours=index)
        upsert_post({"post_id": f"om{index}", "post_type": "post",
                     "created_at": created.isoformat(),
                     "text": f"older month {index}, long enough to read back",
                     "metrics": {"views": 100, "likes": 1, "replies": 0},
                     "metrics_updated_at": now.isoformat()})
    for index in range(2):
        created = now - timedelta(days=index)
        upsert_post({"post_id": f"nm{index}", "post_type": "post",
                     "created_at": created.isoformat(),
                     "text": f"this month {index}, long enough to read back",
                     "metrics": {"views": 100, "likes": 1, "replies": 0},
                     "metrics_updated_at": now.isoformat()})


def test_a_named_month_is_counted_not_answered_with_the_lifetime_total() -> None:
    """Measured live on his archive, 2026-08-05: "how many posts did i publish in
    august" answered 95 — his whole history — against a true 6. Same shape as the
    weekly-views bug this file was written for, one aggregate along: the number
    was real and answered a question nobody asked."""
    _seed_across_months()
    now = datetime.now(timezone.utc)
    older = (now.replace(day=1) - timedelta(days=5))
    month_name = older.strftime("%B").lower()

    reply = _facts_question_answer(f"how many posts did i publish in {month_name}")
    assert reply is not None, "the question was not routed at all"
    assert "8 in total" not in reply, f"answered the lifetime total: {reply}"
    assert "6 in total" in reply, reply
    assert older.strftime("%B") in reply, f"the window is not named: {reply}"


def test_no_period_still_answers_all_time() -> None:
    _seed_across_months()
    reply = _facts_question_answer("how many posts have i published")
    assert reply is not None
    assert "8 in total" in reply, reply


def test_a_period_it_cannot_resolve_is_admitted_not_silently_widened() -> None:
    """The failure that hid this: every reply he has happens to fall in one
    month, so counting the whole archive produced the RIGHT number by accident
    and nothing looked broken. An unresolvable period must say so."""
    _seed_across_months()
    reply = _facts_question_answer("how many posts did i publish this quarter")
    assert reply is not None
    assert "all time" in reply.lower(), reply


# --- account questions must not leave the account --------------------------- #

def test_who_is_my_audience_is_never_answered_from_the_web() -> None:
    """Measured live 2026-08-05. Nothing claimed this question, so it fell past
    every branch to `_web_lookup_answer` and came back:

        "I looked it up: How to identify your target audience in 5 steps -
         Adobe (source: adobe.com)"

    A generic marketing article, for a question about his own account, from a
    product whose whole claim is that it reads HIS record.
    """
    _seed()   # this fixture DOES set an audience ("AI builders")
    reply = _facts_question_answer("who is my audience")
    assert reply is not None, "still unclaimed - the web lookup will take it"
    assert "adobe" not in reply.lower() and "looked it up" not in reply.lower()
    assert "ai builders" in reply.lower(), reply


def test_with_no_audience_set_it_says_it_cannot_know() -> None:
    """The commonest real state - his live profile has `audience` empty. Anything
    about who reads him would be invented, so the refusal comes first."""
    _seed()
    set_account_profile({"audience": "", "timezone": "UTC"})
    reply = _facts_question_answer("who is my audience")
    assert reply is not None
    assert "demographics" in reply.lower(), reply
    assert "adobe" not in reply.lower()


def test_the_audience_answer_says_what_it_can_measure() -> None:
    _seed()
    reply = _facts_question_answer("what is my target audience")
    assert reply is not None
    assert "post about" in reply.lower(), reply


def test_growth_advice_is_not_swallowed_by_the_audience_branch() -> None:
    """"how do i grow my audience" asks HOW, not WHO."""
    _seed()
    reply = _facts_question_answer("how do i grow my audience")
    assert reply is not None
    assert "demographics" not in reply.lower(), reply


def test_a_numbered_judgement_reaches_the_measured_verdict() -> None:
    """"is that good" was caught; "is 23 followers good" was not, so it went to
    the model, which answered from nothing - "No, it's not great yet... that
    number needs to grow before you can rely on X for anything serious." Eight
    seconds, no figure from the record, and an external comparison Starling
    cannot make. The honest handler already existed."""
    _seed()
    for question in ("is 23 followers good", "are 23 followers good",
                     "is 300 views good"):
        reply = _facts_question_answer(question)
        assert reply is not None, f"unclaimed, so the model answers it: {question}"
        assert "own history" in reply.lower(), (question, reply)


def test_a_post_critique_is_not_swallowed_by_the_verdict_branch() -> None:
    """"is my post good" is a critique of one post, not a judgement on a count."""
    _seed()
    reply = _facts_question_answer("is my post good")
    assert reply is not None
    assert "own history" not in reply.lower(), reply


def test_a_month_is_counted_in_his_calendar_not_in_utc() -> None:
    """A post made at 21:00 on 31 July in New York is 01:00 on 1 August in UTC.
    Built in UTC it lands in the wrong month, which is the same mistake
    `_display_timezone` exists for: "reported 'you posted ~19:24 UTC on Monday'
    for a post the user made on SUNDAY evening."

    None of his 60 dated posts hit that boundary yet, so this was latent - but
    his measured peak is 16:00 local and he has a post at 20:06, so it was only
    a question of which evening he posted on.
    """
    set_account_profile({"audience": "AI builders", "timezone": "America/New_York"})
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "boundary-1", "post_type": "post",
                 # 21:00 on 31 July, New York.
                 "created_at": "2026-08-01T01:00:00.000Z",
                 "first_observed_at": now.isoformat(),
                 "text": "an evening post on the last day of the month, long enough",
                 "metrics": {"views": 100, "likes": 1},
                 "metrics_updated_at": now.isoformat()})

    july = _facts_question_answer("how many posts did i publish in july")
    august = _facts_question_answer("how many posts did i publish in august")
    assert july and "1 in total" in july, f"the boundary post was not counted as July: {july}"
    assert august and "nothing on record" in august.lower(), (
        f"the boundary post leaked into August: {august}")


def _seed_one(views: int, likes: int) -> None:
    """One recent post with exactly these numbers, so it is the 'latest'."""
    set_account_profile({"audience": "AI builders",
                         "content_pillars": ["AI models"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "solo", "post_type": "post",
                 "created_at": (now - timedelta(minutes=7)).isoformat(),
                 "text": "a body long enough to be read back in the answer",
                 "metrics": {"views": views, "likes": likes, "replies": 0},
                 "metrics_updated_at": now.isoformat()})


def test_a_post_below_the_rate_floor_never_renders_none_percent() -> None:
    """THE CROSS-LANE COUPLING, and the reason neither half ships alone.

    `account_facts` withholds `engagement_rate_pct` below a view floor, because
    one like on three views is 33.33% and reads as a verdict. This line was
    guarded on `interactions`, not on the rate — so it interpolated the withheld
    value and put "(None%)" on screen for EXACTLY the post that motivated the
    floor: his 3-view, 1-like Spider-Man post. A fix that removed a wrong number
    would have shipped a broken one, one layer down.
    """
    _seed_one(views=3, likes=1)
    # PHRASING MATTERS AND IT IS NOT OBVIOUS. "what was my latest post" is
    # answered by an earlier branch entirely; only these reach the recorded-post
    # renderer that carries the rate. Verified by probing all eleven phrasings —
    # a test on the wrong one passes while the guarded line never executes.
    reply = _facts_question_answer("tell me about my last post")
    assert reply is not None
    assert reply.startswith("Your most recent recorded"), \
        f"this phrasing must reach the guarded renderer, got: {reply[:60]}"
    assert "None" not in reply, reply
    assert "%" not in reply, reply
    # The count is true and complete on its own — that part must survive.
    assert "3 views" in reply, reply
    assert "1 interactions" in reply, reply


def test_above_the_floor_the_percentage_still_shows() -> None:
    """The guard must not silence a rate that is actually supported."""
    from app.account_facts import _RATE_MIN_VIEWS
    _seed_one(views=_RATE_MIN_VIEWS * 10, likes=1)
    reply = _facts_question_answer("tell me about my last post")
    assert reply is not None
    assert reply.startswith("Your most recent recorded"), reply[:60]
    assert "%" in reply, reply
    assert "None" not in reply, reply
