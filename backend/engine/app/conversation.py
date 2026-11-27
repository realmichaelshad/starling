"""ConversationState — where AM I in this conversation, before anything drafts.

The decider, the writer, and every entry point (✦ decide, Create button, chat
intents) were each reading fragments of the situation and missing the rest:
the Tibo incident drafted a SECOND reply to a 15h-old thread the user had
already replied to, because the one path that ran only checked a DB row and the
button path checked nothing at all.

This is the single object that answers, for any request:
  - whose post is this (mine / my thread / quotes me / a stranger's)?
  - what have I ALREADY done here (from the DB and from what's on screen)?
  - how old is it, and how crowded?
  - what is the quoted chain underneath it?

Computed once per request; every gate reads the same truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .schemas import PostContext
from .storage import find_recorded_reply_to_post


@dataclass
class ConversationState:
    surface: str = "post"                 # what the user is about to do
    ownership: str = "stranger"           # own_post | own_thread | quotes_me | stranger
    my_prior_replies: int = 0             # recorded + visible evidence, deduped
    recorded_reply: dict | None = None    # the DB row, if one exists
    visible_own_reply: bool = False       # my reply is on screen in this thread
    thread_scanned: bool = False          # the extension actually read the thread
    post_age_minutes: float | None = None
    crowd_size: int | None = None         # visible reply count on the target
    quoted_chain: list[str] = field(default_factory=list)

    @property
    def already_engaged(self) -> bool:
        """Have I already replied here, by ANY evidence?

        The DB row alone is not truth: it goes stale when the user deletes a
        reply on X. When the extension scanned the thread, what is VISIBLE wins
        in both directions — a visible reply blocks even without a DB row, and
        a scanned thread with no visible reply un-blocks a stale row.
        """
        if self.thread_scanned:
            return self.visible_own_reply
        return self.visible_own_reply or self.recorded_reply is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface, "ownership": self.ownership,
            "already_engaged": self.already_engaged,
            "my_prior_replies": self.my_prior_replies,
            "thread_scanned": self.thread_scanned,
            "post_age_minutes": round(self.post_age_minutes, 1)
            if self.post_age_minutes is not None else None,
            "crowd_size": self.crowd_size,
            "quoted_chain_depth": len(self.quoted_chain),
        }


# THIS COPY WAS THE WEAKER ONE and sat under ownership detection: it consulted
# `get_account_profile` alone, so an install with an unset profile handle got ""
# and never recognised its own thread. Now the same function as everywhere else.
from .identity import own_handle as _own_handle


def _age_minutes(context: PostContext) -> float | None:
    raw = getattr(context, "post_created_at", None)
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 60.0)
    except (ValueError, TypeError):
        return None


# Reply-decay: "when do replies die down" — the user's explicit ask. The
# visible thread carries its own answer in the reply timestamps the extension
# now scrapes. Temperatures, from the NEWEST visible reply's age:
#   hot      — fresh replies in the last ~30 min: the room is live, reply NOW
#   warming  — activity within ~90 min: still worth entering
#   cooling  — last reply 1.5–6 h ago: the room is emptying
#   dead     — nothing for 6+ h (or an old post with no replies at all):
#              a reply lands in an empty room
#   unknown  — the thread was not scanned (timeline view), no read available
_THREAD_HOT_MIN = 30.0
_THREAD_WARM_MIN = 90.0
_THREAD_DEAD_MIN = 360.0


def thread_temperature(context: PostContext | None) -> tuple[str, str]:
    """(temperature, one plain line for the card). Never raises."""
    try:
        if context is None or not getattr(context, "thread_scanned", False):
            return "unknown", ""
        ages = [float(a) for a in (getattr(context, "visible_reply_ages_minutes", None) or [])
                if isinstance(a, (int, float)) and a >= 0]
        age = None
        raw = getattr(context, "post_created_at", None)
        if raw:
            age = _age_minutes(context)
        if not ages:
            if age is not None and age > 720:
                return "dead", "No visible replies and the post is old — this room is empty."
            return "unknown", ""
        newest = min(ages)
        recent = sum(1 for a in ages if a <= _THREAD_WARM_MIN)
        if newest <= _THREAD_HOT_MIN and recent >= 2:
            return "hot", "Replies are landing right now — a reply here gets seen."
        if newest <= _THREAD_WARM_MIN:
            return "warming", "The thread is still active — worth entering soon."
        if newest <= _THREAD_DEAD_MIN:
            hours = newest / 60.0
            return "cooling", f"The last reply was ~{hours:.0f}h ago — this thread is winding down."
        hours = newest / 60.0
        return "dead", (f"The last reply was ~{hours:.0f}h ago — replies here have died down, "
                        "so a new one lands in an empty room.")
    except Exception:
        return "unknown", ""


def conversation_state(context: PostContext | None, action: str = "reply") -> ConversationState:
    """The one situational read every path shares. Never raises."""
    state = ConversationState(surface=str(action or "reply"))
    if context is None:
        return state
    try:
        own = _own_handle()
        author = str(getattr(context, "author_handle", "") or "").lstrip("@").lower()
        parent = str(getattr(context, "parent_author", "") or "").lstrip("@").lower()
        quoted = str(getattr(context, "quote_text", "") or "").strip()

        if getattr(context, "is_own_post", False) or (own and author == own):
            state.ownership = "own_post"
        elif own and parent == own:
            state.ownership = "own_thread"
        elif own and quoted and own in quoted.lower():
            state.ownership = "quotes_me"

        state.thread_scanned = bool(getattr(context, "thread_scanned", False))
        state.visible_own_reply = bool(getattr(context, "visible_own_reply", False))
        try:
            state.recorded_reply = find_recorded_reply_to_post(
                getattr(context, "post_id", None))
        except Exception:
            state.recorded_reply = None
        state.my_prior_replies = int(state.visible_own_reply) or int(
            state.recorded_reply is not None)

        state.post_age_minutes = _age_minutes(context)
        count = getattr(context, "visible_reply_count", None)
        state.crowd_size = int(count) if isinstance(count, (int, float)) else None
        if quoted:
            state.quoted_chain.append(quoted)
    except Exception:
        pass  # a partial read is still a read; gates degrade honestly
    return state
