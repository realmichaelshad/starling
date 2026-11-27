"""Which posts were PAID for, so the account never learns from its own ad spend.

On 2026-07-26 the drafting rules were rewritten from measured performance:
predictions 3.0x, first person 1.8x, jokes 9.0x. He then said the top post was
boosted. Re-ranked by engagement instead of views, the prediction and
first-person findings inverted — both were drags. Two of five new rules had
been derived from his ad budget.

Nothing in the capture records a boost. There is no field for it anywhere in
the schema, so a promoted post and an organically viral post are literally
indistinguishable to every learning path in this product: the scorer, the
timing model, the style model and the angle bandit all read views.

That is a slow poison. Boosting works — that is what it is for — so the posts
he pays to promote will always look like his best work, and the account will
keep being taught to write more of whatever he happened to promote.

This keeps the list. It is deliberately a small side-table keyed by post id
rather than a schema change, so it can be populated from anywhere (the X
analytics page, a manual mark in the dock, an import) without a migration.
"""
from __future__ import annotations

from typing import Any

from .storage import get_job_state, set_job_state

_KEY = "boosted_post_ids"


def boosted_ids() -> set[str]:
    """Post ids known to have been promoted."""
    stored = get_job_state(_KEY) or {}
    return {str(i) for i in (stored.get("ids") or []) if str(i).strip()}


def mark_boosted(post_id: str, boosted: bool = True) -> set[str]:
    """Record (or clear) a post as promoted. Returns the full set."""
    post_id = str(post_id or "").strip()
    if not post_id:
        return boosted_ids()
    ids = boosted_ids()
    if boosted:
        ids.add(post_id)
    else:
        ids.discard(post_id)
    set_job_state(_KEY, {"ids": sorted(ids)})
    return ids


def is_boosted(row: dict[str, Any]) -> bool:
    return str(row.get("post_id") or "") in boosted_ids()


def organic_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows any learning path should actually read.

    Every caller that ranks by reach should go through here. A boosted post is
    not a bad post — it just cannot testify about what earns attention.
    """
    ids = boosted_ids()
    if not ids:
        return list(rows)
    return [r for r in rows if str(r.get("post_id") or "") not in ids]


def engagement(row: dict[str, Any]) -> float:
    """Likes + reposts + replies.

    The honest outcome measure while boosts are unlabelled: promotion buys
    impressions far more readily than it buys someone caring enough to reply.
    Ranking his posts this way inverted two of the five findings that views had
    produced, which is how the contamination was caught.
    """
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return 0.0
    return sum(float(metrics.get(key) or 0.0)
               for key in ("likes", "reposts", "replies"))


def views(row: dict[str, Any]) -> float:
    """Impressions. Treat as UNRELIABLE for learning unless the post is known
    organic — see the module docstring."""
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return 0.0
    for key in ("views", "impressions", "impression_count"):
        if metrics.get(key):
            return float(metrics[key])
    return 0.0
