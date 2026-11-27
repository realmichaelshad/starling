"""`own_handle` has callers that compare against it, so its shape is a contract.

Lane Q raised this against their own code, which is the reason it is worth a
test: `intelligence.py` normalises a parent handle with
`lstrip("@").strip().lower()` and compares it to `own_handle()`. If `own_handle`
ever stops lowercasing — or starts returning the leading "@" — that comparison
does not fail loudly. It silently stops matching, and the guard that keeps a
draft from claiming the author's possessions on HIS OWN post quietly dies.

A comment saying "keep these in sync" is the thing that does not survive the
next edit. This is the same fact, checkable.

There were TWO copies of this function before the consolidation, and they had
already diverged: `conversation.py`'s dropped the `infer_account_model` fallback
while `main.py`'s kept it, so on an install with an unset profile handle one
returned the handle and the other returned "". That copy fed ownership detection
(`own_post`, `own_thread`), which is exactly what a self-reply guard keys off.
Hence the last test here: one function, not two that agree today.
"""
from __future__ import annotations

from app import conversation, main
from app.identity import normalize_handle, own_handle


def test_it_never_raises():
    """Every caller is on a drafting path. A profile that cannot be read is not
    a reason to stop writing, so this returns "" rather than propagating."""
    assert isinstance(own_handle(), str)


def test_the_shape_the_comparers_rely_on(monkeypatch):
    """Lowercased, no leading @, no surrounding space — the exact normalisation
    `intelligence.py` applies to the parent handle before comparing."""
    monkeypatch.setattr("app.storage.get_account_profile",
                        lambda: {"handle": "  @Sample_Writer  "})
    got = own_handle()
    assert got == "sample_writer", got
    # CALL the shared function rather than restating its character work. This
    # line used to re-derive the normalisation inside the assertion — a fourth
    # implementation, in the test whose whole job is to pin the contract.
    assert got == normalize_handle(got)


def test_the_archive_fallback_is_consulted(monkeypatch):
    """THE DIVERGENCE THAT HAD ALREADY SHIPPED. `conversation.py`'s copy read the
    profile alone, so an install that never recorded a handle got "" from it and
    the real handle from `main.py`'s copy — and `conversation.py` is where
    ownership is decided."""
    monkeypatch.setattr("app.storage.get_account_profile", lambda: {"handle": ""})
    monkeypatch.setattr("app.account_model.infer_account_model",
                        lambda: {"handle": "@FromTheArchive"})
    assert own_handle() == "fromthearchive"


def test_an_unreadable_profile_is_empty_not_an_exception(monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.storage.get_account_profile", boom)
    assert own_handle() == ""


def test_there_is_exactly_one_implementation():
    """Not "two that currently agree". The consolidation is the fix; this is
    what stops a future edit from reintroducing a second copy that drifts."""
    assert main._own_handle is own_handle
    assert conversation._own_handle is own_handle


# --------------------------------------------------------------------------- #
# one normalizer, called — not four, agreeing
# --------------------------------------------------------------------------- #

def test_every_caller_uses_the_shared_normalizer():
    """Four hand-rolled copies existed and two had diverged. `own_handle` and
    `intelligence.py`'s call site agreed only by luck; `pack_ownership`'s was
    missing the trailing strip; this file re-derived it in an assertion."""
    import inspect

    from app import intelligence, pack_ownership

    import re

    # STRIP DOCSTRINGS AS WELL AS COMMENTS. A prose mention is not an
    # implementation, and these modules DESCRIBE the old expression in order to
    # explain why it was wrong — a scan that only drops `#` lines trips on the
    # very documentation that records the fix. (Twice now: the same shape caught
    # the `!= "public"` check in test_protected_stays_local.)
    source = inspect.getsource(pack_ownership)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert 'lstrip("@")' not in body, (
        "pack_ownership still hand-rolls the normalisation instead of calling "
        "identity.normalize_handle")

    # The call site in intelligence goes through it too; test_borrowed_possessive
    # pins the same fact from the other side.
    call_site = inspect.getsource(intelligence._assemble_candidates)
    assert "normalize_handle(parent)" in call_site

    # KNOWN FIFTH COPY, deliberately not asserted here: intelligence.py:3600,
    # `author = (author_handle or "").lstrip("@").lower()` inside the mention
    # repair — no strip at all, so a padded handle keeps BOTH the newline and
    # the "@", and the comparison below it never matches. Different function,
    # another lane's file, outside the one-line grant that produced this change:
    # reported rather than edited. Fold it in and this exception goes away.


def test_the_shared_normalizer_handles_what_broke_before():
    """The padded shape X's User-Name DOM yields, and the case the
    pack_ownership copy got wrong."""
    assert normalize_handle("\n@BenPosts  ") == "benposts"
    assert normalize_handle("@ name") == "name"      # pack_ownership returned " name"
    assert normalize_handle(None) == ""
    assert normalize_handle("@@x") == "x"
