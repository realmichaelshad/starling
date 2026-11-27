"""The registry of everything Starling can do, and how a person reaches it.

WHY THIS EXISTS. Starling was far more capable than it was reachable. The
engine had ~60 endpoints once; chat understood three intents and the dock had
five buttons. Whole finished features were invisible:

  * `assess_opportunity()` has always returned a live momentum verdict for your
    own post — "taking off, answer the best replies now, ~3.2x your median" —
    and nothing could ask for it.
  * The image pipeline never returned a single image for anyone, for the whole
    life of the product, behind a swallowed 401.
  * The self-reply button was deliberately hidden.

An unreachable feature is indistinguishable from a broken one, and no test could
tell the difference because no test asked "can a person get here?".

So every capability is declared here ONCE, with the sample phrases that must
reach it. `tools/capability_probe.py` walks this registry and fails if any entry
stops resolving. Adding a capability without a door now breaks the build.

Handlers are deterministic and answer from real measurements. When there is not
enough data they say so plainly — an honest "not yet" beats a confident guess,
and every helper below already reports its own evidence level.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Capability:
    """One thing Starling can do, and the words that get you there."""

    key: str
    summary: str
    matcher: re.Pattern
    handler: Callable[[str, dict | None], str | None]
    # Phrases that MUST route here. This is the contract the probe enforces.
    samples: tuple[str, ...] = ()
    # True when the answer needs an open post (the dock attaches one).
    needs_context: bool = False
    # Where else it is reachable, for the help menu and the probe's report.
    ui_routes: tuple[str, ...] = ()


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return "?"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def _boost_answer(_message: str, context_data: dict | None) -> str | None:
    """Is this post worth amplifying right now, and how?

    Fully built in opportunities.assess_opportunity (own-post branch) and never
    reachable before: it returns `follow_up` with a momentum verdict measured
    against THIS account's own median, not a generic threshold.
    """
    if not context_data:
        return ("Open the post you want to boost and press the ✦ on it, or attach "
                "it to this chat — then I can read its live momentum.")
    from .opportunities import assess_opportunity
    from .schemas import PostContext

    try:
        context = PostContext(**{k: v for k, v in context_data.items()
                                 if k in PostContext.model_fields})
    except Exception:
        return "I could not read that post well enough to judge its momentum."
    if not context.is_own_post:
        return ("That is not your post, so there is nothing to boost. I can draft "
                "a reply or a quote of it instead.")
    try:
        read = assess_opportunity(context)
    except Exception:
        return "I could not measure that post's momentum just now."

    lines = [str(read.get("headline") or "Follow the discussion, not the metric")]
    rationale = str(read.get("rationale") or "").strip()
    if rationale:
        lines.append(rationale)
    for reason in (read.get("reasons") or [])[:3]:
        lines.append(f"• {reason}")
    lines.append("Say 'self-reply' and I'll draft the follow-up, or 'self-quote' "
                 "to re-frame it for a new audience.")
    return "\n".join(lines)


def _autopsy_answer(_message: str, context_data: dict | None) -> str | None:
    """How did a post actually do, measured against this account's own history."""
    if not context_data or not context_data.get("post_id"):
        return ("Open the post you want me to look at and press ✦, or attach it "
                "here — I need the actual post to read its numbers.")
    from .performance import diagnose_post

    try:
        report = diagnose_post(str(context_data["post_id"]))
    except Exception:
        report = None
    if not report:
        return ("I have no measured history for that post yet. Run Sync so I can "
                "pull its numbers, then ask me again.")

    lines = []
    verdict = report.get("verdict") or report.get("summary")
    if verdict:
        lines.append(str(verdict))
    for key, label in (("reach", "Reach"), ("conversation", "Conversation"),
                       ("resonance", "Resonance")):
        value = report.get(key)
        if isinstance(value, dict) and value.get("percentile") is not None:
            lines.append(f"• {label}: {_pct(value['percentile'])}th percentile "
                         f"against your own posts ({value.get('evidence', 'limited')} evidence)")
    for note in (report.get("notes") or [])[:2]:
        lines.append(f"• {note}")
    return "\n".join(lines) if lines else (
        "I have that post but not enough comparable history to judge it yet.")


def _timing_answer(_message: str, _context_data: dict | None) -> str | None:
    """When this account actually gets reach — measured, never generic advice.

    Posts and replies are answered SEPARATELY (they live on different clocks):
    posts by the account's own measured windows, replies by the decay principle
    plus each thread's live temperature.
    """
    from .performance import timing_recommendations
    from .storage import get_account_profile

    try:
        profile = get_account_profile()
        timezone_name = profile.get("timezone", "UTC")
        posts = timing_recommendations("reach", timezone_name, post_type="post")
        replies = timing_recommendations("reach", timezone_name, post_type="reply")
    except Exception:
        return "I could not read your posting history to work out a window."

    lines: list[str] = []
    windows = posts.get("windows") or []
    if windows:
        lines.append(f"POSTS · measured from {posts.get('sample_size', '?')} of yours "
                     f"({posts.get('confidence', 'limited')} confidence, {timezone_name}):")
        for window in windows[:3]:
            lines.append(f"• {window.get('label') or window.get('window')} · "
                         f"{window.get('note') or 'above your median'}")
    else:
        lines.append("POSTS: " + str(posts.get("reason") or "not enough dated posts yet "
                     "to name a window.") + " I will not invent a best time from nothing.")

    reply_windows = replies.get("windows") or []
    if reply_windows:
        lines.append(f"REPLIES · measured from {replies.get('sample_size', '?')} of yours:")
        for window in reply_windows[:2]:
            lines.append(f"• {window.get('label') or window.get('window')}")
    else:
        lines.append("REPLIES: not enough measured replies for a table yet — but reply "
                     "timing is mostly the THREAD's clock, not yours: replies earn in a "
                     "post's first hour or two and die off after. I read each thread's "
                     "temperature live when you open one.")
    return "\n".join(lines)


def _whats_working_answer(_message: str, _context_data: dict | None) -> str | None:
    """Which formats and topics actually earn, from this account's own numbers."""
    from .performance import performance_insights

    try:
        insights = performance_insights()
    except Exception:
        return "I could not read your performance history just now."

    by_format = insights.get("by_format") or {}
    lines = []
    strongest = insights.get("strongest_format")
    if strongest:
        lines.append(f"Your strongest format is {strongest}.")
    for name, row in by_format.items():
        if not isinstance(row, dict) or not row.get("posts"):
            continue
        lines.append(f"• {name}: {row['posts']} posts, median "
                     f"{_pct(row.get('median_views'))} views "
                     f"({row.get('evidence', 'limited')} evidence)")
    topics = insights.get("top_topics") or insights.get("topics") or []
    for topic in topics[:3] if isinstance(topics, list) else []:
        if isinstance(topic, dict) and topic.get("topic"):
            lines.append(f"• topic '{topic['topic']}': {topic.get('note') or 'measured'}")
    if not lines:
        return ("I do not have enough measured posts to say what is working yet. "
                "Run Sync to pull your history in.")
    lines.append("Evidence levels are honest: 'limited' means treat it as "
                 "directional, not proven.")
    return "\n".join(lines)


def _planner_answer(_message: str, _context_data: dict | None) -> str | None:
    """The next concrete move, from the planner that already exists."""
    from .planner import next_moves

    try:
        plan = next_moves(llm=None, limit=3)
    except Exception:
        return "I could not work out a next move just now."
    moves = plan.get("moves") or []
    if not moves:
        return ("Nothing is worth acting on right now. That is a real answer — "
                "forcing a post into a dead window costs more than waiting.")
    lines = ["Next moves, strongest first:"]
    for move in moves[:3]:
        # A reply move's value is its TARGET, not the word "reply" — the Opus 5
        # miss read "reply: reply — replies acquire attention", which names
        # nothing to act on. Prefer the concrete target every time.
        topic = move.get("topic") or move.get("target_hint") or move.get("move")
        why = str(move.get("why") or "").strip()
        lines.append(f"• {move.get('move', 'post')}: {topic}" + (f" · {why[:120]}" if why else ""))
        if move.get("open"):
            lines.append(f"  open: {move['open']}")
    return "\n".join(lines)


# A goal STATED, not asked about. "my goal is 5000 followers", "I want to get
# to 5k followers", "grow to 1000 followers by december".
_GOAL_STATED = re.compile(
    r"\b(?:my goal is|i want to (?:get|grow) to|i want|goal:|grow to|get to|"
    r"aiming for|target is|trying to (?:get|hit|reach))\s+"
    r"(?:about\s+|around\s+|at least\s+)?"
    r"(\d[\d,\.]*\s*[km]?)\s*"
    r"(followers?|views?|impressions?|replies|profile visits?)?", re.IGNORECASE)

_GOAL_OBJECTIVE = {
    "follower": "followers", "followers": "followers",
    "view": "reach", "views": "reach",
    "impression": "reach", "impressions": "reach",
    "reply": "conversation", "replies": "conversation",
    "profile visit": "profile_visits", "profile visits": "profile_visits",
}

_GOAL_HORIZON_DAYS = 90


def _goal_number(raw: str) -> float | None:
    text = raw.strip().lower().replace(",", "")
    multiplier = 1.0
    if text.endswith("k"):
        multiplier, text = 1_000.0, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000.0, text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _set_goal(message: str) -> str | None:
    """"My goal is 5000 followers" — SET it.

    `POST /goals` exists, `create_goal` exists, and nothing could reach either:
    no route the extension reaches leads to goals, and it never calls one. So
    this capability answered a stated goal with "You have no goal set. Tell me
    one in plain words — 'grow to 5k followers'" — asking for exactly what had
    just been said, in exactly the words it had just been said in, because
    there was no path that could accept it.
    """
    match = _GOAL_STATED.search(message or "")
    if not match:
        return None
    target = _goal_number(match.group(1))
    if target is None or target <= 0:
        return None
    noun = (match.group(2) or "followers").lower().rstrip()
    objective = _GOAL_OBJECTIVE.get(noun, "followers")

    from datetime import datetime, timedelta, timezone
    from .storage import create_goal

    baseline = 0.0
    try:
        from .account_facts import account_facts
        facts = account_facts("")
        if objective == "followers" and facts.get("followers") is not None:
            baseline = float(facts["followers"])
        elif objective == "reach" and facts.get("median_views") is not None:
            baseline = float(facts["median_views"])
    except Exception:                                        # noqa: BLE001
        baseline = 0.0
    if target <= baseline:
        return (f"You're already at {baseline:g} {noun}, so {target:g} isn't a "
                "goal — give me a number above where you are now.")
    horizon = datetime.now(timezone.utc) + timedelta(days=_GOAL_HORIZON_DAYS)
    label = f"{target:g} {noun}"
    # CHANGING YOUR MIND IS NOT A SECOND GOAL. Saying "my goal is 5k followers"
    # twice, or revising it to 10k, must not leave him holding two — the brief
    # picks one active goal and a stale duplicate would silently win half the
    # time. One goal per objective, replaced in place.
    from .storage import list_goals, update_goal

    replaced = False
    try:
        for goal in list_goals() or []:
            if str(goal.get("objective")) == objective and \
                    str(goal.get("status", "active")) == "active":
                update_goal(str(goal.get("id")),
                            {"name": label, "baseline": baseline,
                             "target": target, "horizon_at": horizon.isoformat()})
                replaced = True
                break
    except Exception:                                        # noqa: BLE001
        replaced = False
    if not replaced:
        try:
            create_goal({"name": label, "objective": objective,
                         "baseline": baseline, "target": target,
                         "horizon_at": horizon.isoformat(),
                         "weekly_cadence": {}, "guardrails": []})
        except Exception:                                    # noqa: BLE001
            return None
    lead = "Updated" if replaced else "Set"
    return (f"{lead}: {label}, from {baseline:g} today, by "
            f"{horizon.date().isoformat()} ({_GOAL_HORIZON_DAYS} days — say a "
            "date if you want a different one). I'll hold recommendations "
            "against it.")


def _goals_answer(message: str, _context_data: dict | None) -> str | None:
    from .storage import list_goals

    stated = _set_goal(message)
    if stated:
        return stated
    try:
        # ACTIVE ONLY. `list_goals` returns every status on purpose — the
        # export and the /strategy/goals listing both need archived ones — so
        # each consumer filters. `_set_goal` above does it, `planner.py:44`
        # does it, `strategy.py:40` does it. This was the one that did not.
        #
        # Live 2026-08-07: "what's my goal" answered "Your goals: 5000
        # followers / 5000 followers" from two rows he had ARCHIVED himself.
        # Nothing archives a goal automatically — `main.py:4611` is the only
        # writer and it is user-initiated — so every row here was one he had
        # deliberately removed, and the product read them back to him twice.
        # The duplicate was the visible symptom; reporting deleted goals as
        # current was the defect.
        goals = [goal for goal in (list_goals() or [])
                 if str(goal.get("status", "active")) == "active"]
    except Exception:
        goals = []
    if not goals:
        return ("You have no goal set. Tell me one in plain words — "
                "\"grow to 5k followers\" or \"more replies from AI builders\" — "
                "and I will hold every recommendation against it.")
    lines = ["Your goals:"]
    for goal in goals[:4]:
        lines.append(f"• {goal.get('name') or goal.get('objective') or goal}")
    return "\n".join(lines)


_WAIT_MEANING = (
    "WAIT means nothing scored high enough to be worth your name on it — not "
    "that the post is bad, and not a refusal. Every verdict comes with the "
    "reason it lost, and you can always override it and draft anyway.")


def _decision_record_answer(message: str, _context_data: dict | None) -> str | None:
    """"Why do you say wait so much" — answered from the actual record.

    It used to answer with a feeling: "Because I'm trying to help you think.
    You're the driver, I'm just the navigator." That is not an answer, and the
    data to give a real one is now written on every decision.
    """
    from .storage import recent_opportunity_assessments

    try:
        rows = recent_opportunity_assessments(limit=200)
    except Exception:                                        # noqa: BLE001
        rows = []
    if not rows:
        return (_WAIT_MEANING + " I have no decisions on record yet, so I "
                "can't tell you how often I've said it — press ✦ on a few "
                "posts and ask again.")
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("action") or "wait")] = counts.get(
            str(row.get("action") or "wait"), 0) + 1
    held = sum(count for action, count in counts.items()
               if action in ("wait", "skip", "research"))
    acted = sum(count for action, count in counts.items()
                if action in ("reply", "quote", "post", "follow_up"))
    total = held + acted
    if not total:
        return _WAIT_MEANING
    reasons: dict[str, int] = {}
    for row in rows:
        if str(row.get("action") or "") not in ("wait", "skip", "research"):
            continue
        for reason in (row.get("reasons") or [])[:1]:
            text = str(reason).strip()
            if text:
                reasons[text] = reasons.get(text, 0) + 1
    line = (f"{_WAIT_MEANING} On the last {total} posts you asked me about I "
            f"held back on {held} and moved on {acted}.")
    top = sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:3]
    if top:
        line += " The reasons I gave most:"
        for text, count in top:
            line += f"\n• {text} ({count}×)"
    return line


def _account_review_answer(message: str, _context_data: dict | None) -> str | None:
    """"What do you think of my account, and what should I do about it?"

    The whole family had no handler and fell to the model — the path that said
    "You're not growing" over a payload showing +5 followers. Worse, with every
    account handler declining, his question reached the preference gate and came
    back "Noted — 'Make it better.' I'll hold every draft to that."
    """
    from .account_review import account_review_text

    # A STATED GOAL IS A DIFFERENT REQUEST. "I want to get to 5k followers"
    # reads as a growth question but is an instruction to record something.
    stated = _set_goal(message)
    if stated:
        return stated
    try:
        return account_review_text()
    except Exception:                                        # noqa: BLE001
        return None


def _speed_answer(_message: str, _context_data: dict | None) -> str | None:
    """"Why is Starling slow" — say what a draft actually costs, not a denial.

    Drafting runs on Starling's hosted AI service, so there is no machine setting
    to blame or tune. What takes time is the number of steps in a draft, and the
    honest answer names them and says what answers instantly.
    """
    return ("Drafts are written by Starling's AI service. A draft is several steps: "
            "reading the post, deciding the point, writing, and checking the result "
            "before you see it, so a full draft usually takes a few seconds and the "
            "first words stream in as soon as writing starts. Anything about your own "
            "account (posts, numbers, timing, goals) I answer from your data in under "
            "a second, with no AI call at all.")


def _reminder_answer(_message: str, _context_data: dict | None) -> str | None:
    """"Remind me to post at 3pm" — say what scheduling here actually is.

    The model answered "I'll remind you when the time comes", which is a
    promise nothing can keep: scheduling operates on a DRAFT (`mode=schedule`,
    ops save/list_due/mark_posted), so there is no such thing as a bare
    reminder with nothing attached. He would have waited for 3pm and got
    nothing, and only found out by being let down.
    """
    return ("I can't set a bare reminder — what I can hold is a DRAFT. Write "
            "the post first (\"write me a post about X\"), then say \"hold "
            "this until 3pm\" and I'll keep it and surface it when it's due. "
            "You still press Post yourself.")


def _can_you_post_answer(_message: str, _context_data: dict | None) -> str | None:
    """"Can you post for me" — the one boundary that must never be fudged.

    The model answered "Sure. What do you want to post about?" That is a false
    capability claim about the exact thing the whole architecture exists to
    prevent: model output can never reach a submit control, and the human
    presses Post. Someone who believes "sure" will stop reading drafts before
    they go out, which is precisely the trust the guard was built to keep.
    """
    return ("No — I can't post, and I'm not built to. I draft it, you read it, "
            "you press Post. Nothing I write can reach the send button on its "
            "own. Tell me what you want to say and I'll write it.")


def _export_answer(_message: str, _context_data: dict | None) -> str | None:
    # A privacy answer is the worst possible place to overstate, because it is the
    # moment the user is deciding whether to believe the rest. The hosted facts
    # (docs/SECURITY-AND-PRIVACY.md): one engine and one data folder per customer on
    # Starling's server, prompts to the AI provider through Starling's service, an
    # export on the account page (web/templates/account/data.html), and account
    # deletion that deletes the folder. (API-native pass: the desktop wording, "all
    # local, in <file>, nothing is uploaded, no account", described the retired app.)
    return ("Your posts, metrics, profile and drafts are kept in your own Starling "
            "database on Starling's server, in a folder no other customer's engine can "
            "read. To draft, Starling sends the prompt (the post and your saved writing "
            "and preferences) to its AI provider through Starling's own service. You can "
            "export your data from your account page, and deleting your account deletes it.")


# --------------------------------------------------------------------------- #
# The registry. Adding a capability without samples is a build failure.
# --------------------------------------------------------------------------- #

CAPABILITIES: tuple[Capability, ...] = (
    # FIRST, because it is a boundary and not a feature. Anything below it that
    # happened to match "post" would answer a capability question with a draft.
    Capability(
        key="can_you_post",
        summary="Can you post for me?",
        # Narrow on purpose. "Can you post ABOUT opus 5" is a request to draft,
        # and "can you reply to this" is the product's main verb — neither is a
        # question about the boundary. This matches only the on-my-behalf sense:
        # an explicit qualifier, or the bare question with nothing after it.
        matcher=re.compile(
            r"\b(?:can|could|will|would|do|does)\s+(?:you|u|it|starling)\s+"
            r"(?:actually\s+|auto[- ]?|just\s+)?(?:post|tweet|publish|submit)\b"
            r"(?:\s+(?:it|this|that|them))?"
            r"\s*(?:for\s+me|on\s+my\s+behalf|automatically|automatic|yourself|"
            r"by\s+yourself|itself|directly|without\s+me)\b"
            r"|\b(?:can|could|will|would)\s+(?:you|u)\s+(?:actually\s+)?"
            r"(?:post|tweet|publish|submit)(?:\s+(?:it|this|that))?\s*[?.!]*$"
            r"|\bpost\s+(?:it\s+)?(?:for\s+me|on\s+my\s+behalf)\b"
            r"|\bdo\s+you\s+(?:ever\s+)?post\s+anything\s+(?:yourself|on\s+your\s+own)\b",
            re.IGNORECASE),
        handler=_can_you_post_answer,
        samples=("can you post for me", "will you post it", "post it for me",
                 "can you tweet this for me", "do you post automatically",
                 "can you post"),
    ),
    Capability(
        key="account_review",
        summary="How is my account doing, and what should I do about it?",
        # Deliberately broad, because this is how he actually asks. Every one of
        # these reached the model before, and the model is the component that
        # editorialises past its own data on exactly this kind of question.
        matcher=re.compile(
            r"\bwhat do you think of (?:my|the) account\b"
            r"|\bhow('?s| is) my account\b.{0,20}\b(?:doing|going|looking)\b"
            r"|\b(?:what|how) (?:should|can|do) i .{0,30}\b(?:better|improve|grow|"
            r"bigger|more followers|more views|more reach)\b"
            r"|\bhow do i (?:grow|improve|get bigger|get more (?:views|followers|reach))\b"
            r"|\bwhat(?:'?s| is) (?:holding|stopping|blocking) (?:me|my account)\b"
            r"|\bwhat am i doing wrong\b|\bwhat(?:'?s| is) my biggest (?:problem|issue|weakness)\b"
            r"|\bhow do i (?:go viral|blow up)\b|\bwhy (?:is|isn'?t) my account\b"
            r"|\bhonest(?:ly)? (?:read|opinion|take) (?:of|on) my account\b"
            r"|\breview my account\b|\baudit my account\b"
            r"|\bwhat should i (?:be )?(?:doing|focus on)\b",
            re.IGNORECASE),
        handler=_account_review_answer,
        samples=("what do you think of my account", "how do i grow my account",
                 "what's holding my account back", "what am i doing wrong",
                 "how do i get more views", "review my account"),
    ),
    Capability(
        key="speed",
        summary="Why is this slow?",
        matcher=re.compile(
            r"\b(?:why (?:are you|is it|is this|is starling)|why so)\s+"
            r"(?:so\s+)?(?:slow|laggy|sluggish|taking so long)\b"
            r"|\bwhy (?:does|is) (?:it|this|starling) take so long\b"
            r"|\byou'?re (?:so )?slow\b|\bthis is (?:so )?slow\b"
            r"|\bhow fast (?:are you|is it)\b|\bwhat model are you (?:using|on)\b",
            re.IGNORECASE),
        handler=_speed_answer,
        samples=("why is starling slow", "why are you so slow",
                 "why does it take so long", "what model are you using"),
    ),
    Capability(
        key="reminder",
        summary="Can you remind me to post later?",
        matcher=re.compile(
            r"\bremind me\b|\bset a reminder\b|\bping me\b|\bnudge me\b"
            r"|\bcan you remember to\b", re.IGNORECASE),
        handler=_reminder_answer,
        samples=("remind me to post at 3pm", "set a reminder", "ping me later"),
    ),
    Capability(
        key="decision_record",
        summary="What does WAIT mean, and how often do you say it?",
        matcher=re.compile(
            r"\bwhat does (?:it mean when you say )?[\"']?wait[\"']? mean\b"
            r"|\bwhat do you mean by wait\b"
            r"|\bwhy (?:do|are) you (?:always |so )?(?:say|saying|keep saying)\s+"
            r"[\"']?wait[\"']?"
            r"|\bwhy (?:do you )?(?:say|keep saying) wait so (?:much|often)\b"
            r"|\bhow often do you (?:say|choose) wait\b"
            r"|\byou (?:always|keep) (?:say|saying) wait\b"
            r"|\bwhy so many waits?\b", re.IGNORECASE),
        handler=_decision_record_answer,
        samples=("what does wait mean", "why do you say wait so much",
                 "why do you always say wait", "how often do you say wait",
                 "you always say wait"),
    ),
    Capability(
        key="boost",
        summary="Should I amplify this post right now?",
        matcher=re.compile(
            r"\bboost\b|\bbump\b|\bamplify\b|\btaking off\b|\bpopping off\b"
            r"|\bhow is (this|my) post doing\b|\bshould i follow ?up\b", re.IGNORECASE),
        handler=_boost_answer,
        samples=("boost this", "is this taking off", "should i bump this post",
                 "should i follow up"),
        needs_context=True,
        ui_routes=("✦ on your own post",),
    ),
    Capability(
        key="autopsy",
        summary="How did this post actually do, and why?",
        matcher=re.compile(
            r"\bhow did (this|it|that post) do\b|\bwhy did (this|it) (flop|fail|bomb)\b"
            r"|\bpost mortem\b|\bautopsy\b|\bhow'?d (this|it) do\b", re.IGNORECASE),
        handler=_autopsy_answer,
        samples=("how did this do", "why did this flop", "how'd it do"),
        needs_context=True,
        ui_routes=("✦ on your own post",),
    ),
    Capability(
        key="timing",
        summary="When should I post?",
        matcher=re.compile(
            r"\bwhen should i post\b|\bbest time to post\b|\bwhat time should i\b"
            r"|\bbest window\b|\bwhen do i get (the most|more) (reach|views)\b",
            re.IGNORECASE),
        handler=_timing_answer,
        samples=("when should i post", "best time to post", "best window"),
    ),
    Capability(
        key="whats_working",
        summary="What is actually working for this account?",
        matcher=re.compile(
            r"\bwhat'?s working\b|\bwhat is working\b|\bwhat should i post more\b"
            r"|\bwhat performs best\b|\bmy best (format|topics?)\b", re.IGNORECASE),
        handler=_whats_working_answer,
        samples=("what's working", "what should i post more of", "what performs best"),
    ),
    Capability(
        key="planner",
        summary="What should I do right now?",
        # "X just came out, what should I post" is a PLANNER question. It used
        # to fall through to the raw trend dump (internal term names and all)
        # on the day of the biggest launch of the year. "what should i post
        # more of" still belongs to whats_working, which is matched earlier.
        matcher=re.compile(
            r"\bwhat should i do (now|next|today)\b|\bnext move\b|\bwhat'?s next\b"
            r"|\bgive me a plan\b|\bwhat should i (post|reply|write|say)\b"
            r"|\bwhat do i (post|reply)\b|\bshould i (post|reply) about\b"
            r"|\bany(thing)? (to|worth) (post|repl)\w*\b", re.IGNORECASE),
        handler=_planner_answer,
        samples=("what should i do now", "what's next", "give me a plan"),
    ),
    Capability(
        key="goals",
        summary="What am I aiming at?",
        # Matched STATING one as well as asking about one. "my goal is 5000
        # followers" hit the old `\bmy goals?\b` alternative and was answered
        # with a request to state a goal.
        matcher=re.compile(
            r"\bmy goals?\b|\bset a goal\b|\bwhat'?s my goal\b"
            r"|\b(?:i want to (?:get|grow) to|grow to|get to|aiming for|"
            r"trying to (?:get|hit|reach))\s+\d", re.IGNORECASE),
        handler=_goals_answer,
        samples=("what's my goal", "my goals", "set a goal",
                 "my goal is 5000 followers", "i want to get to 5k followers"),
    ),
    Capability(
        key="export",
        summary="Where is my data and how do I get it out?",
        matcher=re.compile(
            r"\bexport my data\b|\bwhere is my data\b|\bmy data\b|\bdelete my data\b",
            re.IGNORECASE),
        handler=_export_answer,
        samples=("export my data", "where is my data"),
    ),
)


def find_capability(message: str) -> Capability | None:
    """First capability whose words match. Order is precedence.

    MATCHED AGAINST THE TYPO-NORMALISED COPY TOO, because this file was matching
    raw text only and the most important answer in the product was unreachable
    for the person who owns it.

    "can you post for me" returns "No - I can't post, and I'm not built to. I
    draft it, you read it, you press Post." That is the publish boundary, and a
    model guessing at it is the one wrong answer this product cannot afford.
    Measured against his 1,102 real chat messages: he typed `cna u psot for me`
    NINE times and got none of them, because `psot` never reached the matcher.
    `normalize_typos` turns that exact string into "can you post for me" — the
    correction already existed, nothing here used it.

    Calibrated on the same 1,102 messages: 24 newly reach a capability and every
    one is the right capability — 9 the publish boundary, 6 "how is my account
    doing", 4 "should I amplify this post", 3 "what should I do today", 2 "why do
    you say WAIT". Zero messages change which capability they reach, because the
    raw text is still tried first inside each capability's turn, so precedence is
    exactly what it was.

    Safe by construction as well as by measurement: `normalize_typos` only ever
    corrects a token TOWARD `_CANONICAL`, so it cannot invent an intent that
    shares no shape with what was typed.
    """
    if not message:
        return None
    from .text_normalizer import normalize_typos
    normalised = normalize_typos(message)
    for capability in CAPABILITIES:
        if capability.matcher.search(message) or capability.matcher.search(normalised):
            return capability
    return None


def capability_answer(message: str, context_data: dict | None) -> str | None:
    """Route a message to a capability, or None to let the normal chat run."""
    capability = find_capability(message)
    if capability is None:
        return None
    try:
        return capability.handler(message, context_data)
    except Exception:
        # A capability must never take chat down with it.
        return None


def capability_menu() -> list[str]:
    """One line per capability, for the help answer."""
    return [f"{c.summary} · try \"{c.samples[0]}\"" for c in CAPABILITIES if c.samples]
