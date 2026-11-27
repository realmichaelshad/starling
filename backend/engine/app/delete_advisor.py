"""Delete Advisor (spec §7.5). Reason severity first, never virality.

The previous advisor was driven by reach percentile and post age: it told the
user a post was "too early" to judge, or that it was performing above baseline.
That is the spec's ordering INVERTED. §7.5.6 is explicit —

    user safety and ownership
    outweigh
    reach, engagement, historical data, and growth opportunity

— and §7.5.1 says the product must never use a view count to pressure someone
into leaving a post up. So severity decides first, and reach is shown only when
the decision is genuinely strategic.

Starling never deletes anything. Every path ends with the user acting in X.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import evidence as ev

# §7.5.2 intake reasons. The question is always optional — "Delete it now"
# stays available without answering anything.
REASONS: tuple[tuple[str, str, int], ...] = (
    ("privacy_safety", "Private information, safety, legal, or personal harm", 5),
    ("factual_error", "Material factual error", 4),
    ("wrong_context", "Wrong context or misleading media", 4),
    ("duplicate", "Duplicate or accidental post", 3),
    ("tone_brand", "Tone, brand, or changed opinion", 2),
    ("low_performance", "Low performance or regret", 1),
    ("doing_well_but", "It is performing well, but I still do not want it public", 2),
    ("replace", "I want to replace it with a better version", 2),
    ("other", "Other / no reason needed", 2),
)
_SEVERITY = {key: severity for key, _label, severity in REASONS}

# §7.5.5 actions.
KEEP = "keep"
EDIT = "edit_transparently"
CORRECT = "add_correction"
REPLACE = "draft_replacement"
DELETE = "delete_now"
PAUSE = "pause_and_compare"

# X currently allows editing an eligible original post or quote within an hour.
EDIT_WINDOW_MINUTES = 60

# What X's help says deletion actually does — shown before a strategic delete
# and after an urgent one (§7.5.7), because "it is gone" would be false.
DELETION_SCOPE = (
    "Deleting removes the original post from your account, your followers' "
    "timelines, and X search. Reposts of it go too. Copied text, independent "
    "quote posts, and third-party caches may remain."
)


@dataclass
class DeleteAdvice:
    recommendation: str = PAUSE
    urgency: str = "normal"          # immediate | normal
    reason_key: str | None = None
    delete_priority: float = 0.0
    retention_value: float = 0.0
    headline: str = ""
    alternatives: list[dict[str, str]] = field(default_factory=list)
    considerations: list[str] = field(default_factory=list)
    edit_eligible: bool = False
    deletion_scope: str = DELETION_SCOPE
    show_reach: bool = False
    reach_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation, "urgency": self.urgency,
            "reason": self.reason_key, "delete_priority": self.delete_priority,
            "retention_value": self.retention_value, "headline": self.headline,
            "alternatives": self.alternatives, "considerations": self.considerations,
            "edit_eligible": self.edit_eligible, "deletion_scope": self.deletion_scope,
            "show_reach": self.show_reach, "reach_note": self.reach_note,
            # Never obscured, whatever the recommendation (§7.5.4).
            "delete_now_available": True,
        }


def _age_minutes(post: dict[str, Any], now: datetime | None = None) -> float | None:
    created = ev.parse_time(post.get("created_at"))
    if created is None:
        return None
    return max(0.0, ((now or ev._now()) - created).total_seconds() / 60.0)


def _delete_priority(reason_key: str | None, post: dict[str, Any]) -> float:
    """§7.5.6. Severity of the REASON, plus ownership certainty. Not reach."""
    severity = _SEVERITY.get(reason_key or "other", 2)
    score = severity / 5.0
    # A user who states a reason is exercising ownership; that raises priority
    # rather than inviting a debate about whether they should.
    if reason_key and reason_key != "other":
        score += 0.1
    if post.get("visibility") == "protected":
        score += 0.05
    return round(min(1.0, score), 4)


def _retention_value(post: dict[str, Any], momentum: dict[str, Any] | None) -> float:
    """Continuing reach, conversation and learning value — shown, never used
    to argue against a safety or privacy removal."""
    metrics = post.get("metrics") or {}
    replies = float(metrics.get("replies") or 0)
    quotes = float(metrics.get("quotes") or 0)
    reposts = float(metrics.get("reposts") or 0)
    conversation = min(1.0, (replies + quotes * 2 + reposts) / 20.0)
    moving = 1.0 if (momentum or {}).get("trend") == "accelerating" else 0.0
    return round(min(1.0, conversation * 0.7 + moving * 0.3), 4)


def advise(post: dict[str, Any], *, reason_key: str | None = None,
           momentum: dict[str, Any] | None = None,
           now: datetime | None = None) -> DeleteAdvice:
    """One recommended path, its alternatives, and the honest trade-off."""
    advice = DeleteAdvice(reason_key=reason_key)
    age = _age_minutes(post, now)
    post_type = str(post.get("post_type") or "post")

    # §7.5.5: X allows editing eligible ORIGINAL posts and quotes inside a
    # limited window; replies and several other types cannot be edited.
    advice.edit_eligible = bool(
        age is not None and age <= EDIT_WINDOW_MINUTES
        and post_type in ("post", "quote"))

    advice.delete_priority = _delete_priority(reason_key, post)
    advice.retention_value = _retention_value(post, momentum)

    severity = _SEVERITY.get(reason_key or "other", 2)

    # --- high severity: act immediately, and do NOT make them read a
    # retention argument first (§7.5.6).
    if severity >= 5:
        advice.recommendation = DELETE
        advice.urgency = "immediate"
        advice.headline = "Delete this now."
        advice.considerations = [DELETION_SCOPE]
        advice.alternatives = [
            {"action": "private_record",
             "label": "Keep a private copy before deleting"},
        ]
        return advice

    if severity == 4:
        # A correction may serve the reader better than removal, but deleting
        # remains fully available and is named first when the claim is false.
        advice.recommendation = CORRECT if advice.edit_eligible else DELETE
        advice.headline = ("Correct it transparently, or delete it."
                           if advice.edit_eligible else
                           "Delete it, or publish a correction.")
        advice.alternatives = [
            {"action": EDIT, "label": "Edit within the edit window"}
            if advice.edit_eligible else
            {"action": CORRECT, "label": "Publish a correction or clarification"},
            {"action": DELETE, "label": "Delete the post"},
        ]
        advice.considerations = [
            "A visible correction preserves the record; deletion does not.",
            DELETION_SCOPE,
        ]
        return advice

    if reason_key == "duplicate":
        advice.recommendation = DELETE
        advice.headline = "Delete the duplicate."
        advice.alternatives = [{"action": REPLACE,
                                "label": "Recreate a corrected version later"}]
        advice.considerations = [DELETION_SCOPE]
        return advice

    # --- strategic territory: show both sides and let the user choose.
    advice.show_reach = True
    if reason_key == "doing_well_but":
        advice.recommendation = PAUSE
        advice.headline = "Your call — this is still getting attention."
        advice.reach_note = _reach_note(post, momentum)
        advice.considerations = [
            "That reach is real, but it is not more important than your reason "
            "for removing it.",
            DELETION_SCOPE,
        ]
        advice.alternatives = [
            {"action": KEEP, "label": "Keep it"},
            {"action": CORRECT, "label": "Add context"},
            {"action": DELETE, "label": "Delete it"},
        ]
        return advice

    if reason_key == "low_performance":
        # §7.5.4: the one case where "keep for now" is the default — and it is
        # framed as a measurement decision, never as pressure.
        advice.recommendation = KEEP
        advice.headline = "Keep it for now."
        advice.reach_note = _reach_note(post, momentum)
        advice.considerations = [
            "Early numbers are not a verdict; revise the lesson, not the post.",
            "You can still delete it at any time.",
        ]
        advice.alternatives = [{"action": DELETE, "label": "Delete it anyway"}]
        return advice

    if reason_key == "replace":
        advice.recommendation = REPLACE
        advice.headline = "Draft a replacement first, then delete."
        advice.considerations = [
            "A replacement must be materially new, not a near-duplicate repost.",
            DELETION_SCOPE,
        ]
        advice.alternatives = [{"action": DELETE, "label": "Just delete it"}]
        return advice

    advice.recommendation = PAUSE
    advice.headline = "Compare the options before you decide."
    advice.reach_note = _reach_note(post, momentum)
    advice.considerations = [DELETION_SCOPE]
    advice.alternatives = [
        {"action": KEEP, "label": "Keep it"},
        {"action": CORRECT, "label": "Add an update"},
        {"action": REPLACE, "label": "Draft a replacement"},
        {"action": DELETE, "label": "Delete it"},
    ]
    return advice


def _reach_note(post: dict[str, Any], momentum: dict[str, Any] | None) -> str | None:
    """Reach as information, never as a veto (§7.5.4)."""
    metrics = post.get("metrics") or {}
    views = metrics.get("views")
    if not views:
        return None
    trend = (momentum or {}).get("trend")
    moving = " and still moving" if trend == "accelerating" else ""
    return f"{int(views):,} observed views{moving}."


def after_decision(post_id: str, action: str, *, reason_key: str | None = None) -> dict[str, Any]:
    """§7.5.7. What Starling records once the user has acted in X.

    A deletion is a rejection signal, never proof the post performed badly and
    never evidence that X punished it.
    """
    return {
        "post_id": post_id,
        "action": action,
        "reason": reason_key,
        "record": {
            "excluded_from_voice_examples": action == DELETE,
            "blocks_automatic_reuse": action == DELETE,
            "labelled_failure": False,
            "treated_as_x_penalty": False,
        },
        "note": ("Recorded as a rejection signal only. Starling will ask "
                 "before reusing a similar idea." if action == DELETE else
                 "Decision recorded; the later outcome will be compared."),
    }
