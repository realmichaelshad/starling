"""Which parts of a strategy pack belong to one person.

WIRED at `strategy_v2.exemplars_for_this_install`, which both consumers go
through: `intelligence.PersonalModel` (the voice pool) and `curation.build_dataset`
(the training set).

A pack is mostly craft: briefs, voice traits, banned phrases, length targets.
That travels with the product. Verbatim reply exemplars are different: they are
one account's own words, and on an empty archive they would be the ONLY voice
examples a new customer's first draft sees, so that customer would be written in
someone else's voice.

WHAT IS AND IS NOT PERSONAL:

  reaches a prompt, carries no identity     briefs, voice_traits, banned_phrases,
                                            length_targets (transferable craft)
  reaches a prompt, IS one person           exemplars (verbatim replies)
  parsed but consumed nowhere               tier1_topics, tier2_topics, sections,
                                            hook_families, openers, weak_endings

THE RULE THIS MODULE ENCODES: personal content ships only to the install it
belongs to. The pack says whose it is with an `owner-handle:` line; an install
that is not that account gets the craft and none of the person. A pack that
declares no owner belongs to nobody, so its personal fields reach no install.

The prose is a separate problem with a separate rule: `release_blocker(public=True)`
refuses to ship ANY pack carrying personal identity, owner-declared or not. The
declaration governs who may USE the personal half; it is not permission to put
one person's words on every customer's disk.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# The line a pack uses to declare its owner.
_OWNER_RE = re.compile(r"^\s*owner-handle:\s*@?([A-Za-z0-9_]{1,15})\s*$",
                       re.IGNORECASE | re.MULTILINE)

# Fields carrying one account's identity. Everything not listed here is craft
# and travels with the product. Kept as a NAMED SET rather than a heuristic:
# a future field is personal only when somebody says so, because the failure
# direction matters — withholding craft makes the product worse for everyone,
# while leaking identity is the defect this exists to end.
PERSONAL_FIELDS: frozenset[str] = frozenset({"exemplars"})


def normalize_handle(handle: str | None) -> str:
    """`@Example_` and `example_` are the same account.

    Delegates to `identity.normalize_handle`, which is the one definition.
    """
    from .identity import normalize_handle as _normalize
    return _normalize(handle)


def declared_owner(pack_text: str) -> str:
    """The handle the pack says it belongs to, or "" if it does not say.

    A pack that does not declare an owner is treated as belonging to NOBODY, so
    its personal fields reach no install at all. The failure mode of guessing is
    shipping one person's voice to strangers; the cost of being wrong the other
    way is that a pack author has to add one line.
    """
    match = _OWNER_RE.search(pack_text or "")
    return normalize_handle(match.group(1)) if match else ""


def belongs_here(pack_text: str, local_handle: str | None) -> bool:
    """Is this install the account the pack was written for?

    Both sides must be known. An install that has not yet recorded a handle is
    not a match: "unknown equals everyone" is exactly the failure to avoid.
    """
    owner = declared_owner(pack_text)
    return bool(owner) and owner == normalize_handle(local_handle)


def withhold(strategy_fields: Mapping[str, Any], pack_text: str,
             local_handle: str | None) -> dict[str, Any]:
    """The fields as this install may use them.

    On the owning account this returns them unchanged. Anywhere else the
    personal fields come back empty, and the craft comes back whole.
    """
    if belongs_here(pack_text, local_handle):
        return dict(strategy_fields)
    cleaned = dict(strategy_fields)
    for name in PERSONAL_FIELDS:
        if name in cleaned:
            value = cleaned[name]
            cleaned[name] = type(value)() if isinstance(value, (list, dict, tuple)) else None
    return cleaned


# --------------------------------------------------------------------------- #
# the packaging half: a build gate, not a runtime one
# --------------------------------------------------------------------------- #

_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{2,15}")
_QUOTED_BLOCK_RE = re.compile(r"\n\s*\n\s*\"([^\"]{10,400})\"\s*(?=\n\s*\n|\Z)")


def personal_text_report(pack_text: str, names: Iterable[str] = ()) -> dict[str, Any]:
    """What in this pack identifies a person, for a release to refuse or accept.

    Deliberately a REPORT rather than a scrubber: a pack is prose written by a
    human, and rewriting it automatically would produce a manual nobody
    proofread. The release decides; this only makes the decision informed.
    """
    text = pack_text or ""
    handles = sorted(set(_HANDLE_RE.findall(text)))
    named = {name: text.count(name) for name in names if name and name in text}
    quoted = [match.group(1) for match in _QUOTED_BLOCK_RE.finditer(text)]
    owner = declared_owner(text)
    return {
        "owner": owner,
        "declares_owner": bool(owner),
        "handles": handles,
        "handle_count": len(_HANDLE_RE.findall(text)),
        "named": named,
        "quoted_blocks": len(quoted),
        # A pack carrying identity without saying whose it is, is the shape of
        # the defect: it ships to everyone and belongs to one person.
        "ships_unowned_identity": bool(not owner and (handles or named or quoted)),
    }


def release_blocker(pack_text: str, names: Iterable[str] = (), *, public: bool = True) -> str:
    """Why this pack may not ship, or "" if it may.

    TWO DIFFERENT RULES, and conflating them is how one person's words end up on
    every customer's disk. `withhold` decides who may USE the personal half at
    runtime. This decides whether the file may be DISTRIBUTED, and an owner
    declaration is not a licence for that.

    Returning a sentence rather than a bool on purpose: a release that stops has
    to say what to do, or the next person turns the gate off.
    """
    report = personal_text_report(pack_text, names)
    carries_identity = bool(report["handles"] or report["named"] or report["quoted_blocks"])
    if public:
        if not carries_identity:
            return ""
    elif not report["ships_unowned_identity"]:
        return ""
    found = []
    if report["named"]:
        found.append(", ".join(f"{name} x{count}"
                               for name, count in sorted(report["named"].items())))
    if report["handles"]:
        found.append(f"{report['handle_count']} @-mention(s): "
                     + ", ".join(report["handles"][:6]))
    if report["quoted_blocks"]:
        found.append(f"{report['quoted_blocks']} verbatim quoted block(s)")
    if public:
        owned = f" It declares owner @{report['owner']}, which governs who may USE it at runtime and is not permission to ship it." if report["owner"] else ""
        return ("This pack carries one person's identity and would be installed "
                "on every user's disk: " + "; ".join(found) + "." + owned
                + " Depersonalise the manual before a public release, or ship a "
                  "pack without it.")
    return ("This pack carries one person's identity but does not say whose, so "
            "it would ship to every user: " + "; ".join(found)
            + ". Add an `owner-handle: @<handle>` line so it reaches only that "
              "install, or remove the personal text.")
