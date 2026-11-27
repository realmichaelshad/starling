"""A snapshot from another account cannot repoint who Starling models.

`POST /profile/snapshots` overwrote `handle`, `display_name` and `bio` from any
arriving snapshot with no comparison. The extension's guard is
`isOwnProfilePage` — *the profile of whoever is signed in* — not *the account
Starling models*, so switching X accounts mid-session repointed the identity.
Observed on real data 2026-08-02 (`other_builder`, twice, self-healed on the
next capture). Two minutes for one user; unbounded for anyone with two accounts.

`signed_in_handle` has ridden on every snapshot since the schema was written and
was read by nothing.

THE FRESH-INSTALL CASE IS THE POINT OF THIS FILE. `get_account_profile()`
returns `handle=None` before anything is learned, so a plain equality guard
refuses the first capture and Starling can never discover who it is — while
passing every test that starts from a populated profile. That is the launch
audit's own failure shape (green on the machine where it already works), so the
adopt-then-guard path is proven here rather than assumed.
"""
from __future__ import annotations

import pytest

from app.main import _snapshot_owns_this_account
from app.schemas import ProfileSnapshot


def _snap(handle=None, signed_in=None, display_name=None, bio=None):
    meta = {}
    if signed_in is not None:
        meta["signed_in_handle"] = signed_in
    return ProfileSnapshot(handle=handle, display_name=display_name,
                           bio=bio, source="extension", metadata=meta)


# --------------------------------------------------------------------------- #
# the three states
# --------------------------------------------------------------------------- #

def test_a_fresh_install_adopts_the_first_snapshot():
    """handle=None is how identity is LEARNED. Refusing here bricks onboarding."""
    assert _snapshot_owns_this_account(
        _snap(handle="example_builder", signed_in="example_builder"),
        {"handle": None}) is True


def test_a_fresh_install_adopts_even_an_unfamiliar_handle():
    """There is nothing to compare against yet — whoever is signed in IS the
    account, by definition, on the first capture."""
    assert _snapshot_owns_this_account(
        _snap(handle="anybody", signed_in="anybody"), {"handle": ""}) is True


def test_the_configured_account_is_accepted():
    assert _snapshot_owns_this_account(
        _snap(handle="example_builder", signed_in="example_builder"),
        {"handle": "example_builder"}) is True


def test_another_account_is_refused():
    """The live case: a second X account's profile page arriving mid-session."""
    assert _snapshot_owns_this_account(
        _snap(handle="other_builder", signed_in="other_builder"),
        {"handle": "example_builder"}) is False


# --------------------------------------------------------------------------- #
# the comparison itself — a mismatch here refuses EVERY capture and looks
# identical to the guard working
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stored, arriving", [
    ("example_builder", "@example_builder"),
    ("@example_builder", "example_builder"),
    ("Example_Builder", "example_builder"),
    ("  @Example_Builder ", "@example_builder"),
])
def test_the_at_sign_and_case_do_not_refuse_the_owner(stored, arriving):
    """`PostContext.author_handle` carries the "@" and the stored profile does
    not; both sides go through `identity.normalize_handle` for exactly this."""
    assert _snapshot_owns_this_account(
        _snap(handle=arriving, signed_in=arriving), {"handle": stored}) is True


def test_signed_in_handle_wins_over_the_profile_handle():
    """The snapshot's `handle` is WHOSE PAGE was captured; `signed_in_handle` is
    WHO captured it. Viewing another account's profile while signed in as him
    must not be treated as an identity change."""
    assert _snapshot_owns_this_account(
        _snap(handle="someoneelse", signed_in="example_builder"),
        {"handle": "example_builder"}) is True


def test_an_old_snapshot_without_the_field_is_not_refused():
    """Captures predating `signed_in_handle` cannot be judged from the payload.
    Refusing them would break a working install to defend against something
    undetectable."""
    assert _snapshot_owns_this_account(
        _snap(handle="example_builder"), {"handle": "example_builder"}) is True


def test_the_snapshot_is_still_saved_when_the_overwrite_is_refused():
    """Pinned at the call site: the refusal skips `set_account_profile`, never
    `save_profile_snapshot`. The record stays honest — we saw the capture, we
    just did not let it redefine the account."""
    import inspect
    import re

    from app import main

    source = re.sub(r'"""(?:.|\n)*?"""', "", inspect.getsource(main.profile_snapshot_create))
    save_at = source.index("save_profile_snapshot")
    guard_at = source.index("_snapshot_owns_this_account")
    assert save_at < guard_at, "the snapshot must be saved before the guard decides"
