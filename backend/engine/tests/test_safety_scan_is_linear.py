"""The safety scan reads what strangers post, so its running time must not depend on them.

Found by the security review. The `credentials` rule was one expression whose third branch
(`secret-shaped token`, then up to 60 same-clause characters, then `api key`) backtracked
quadratically over any long run of `[A-Za-z0-9+/]`: a base64 blob, a hash dump, a long URL path.
Measured before the fix: 3,200 characters took 13 seconds; 30,000 (one long post) about twenty
minutes; 100,000 (the most this module scans) about four hours, holding the interpreter lock, so
the customer's engine answered nothing at all, not even a cancel. One post was enough.

`_CredentialsRule` anchors on the rare thing (`api key`) and looks either side of it instead. These
tests pin three things: it is fast on hostile input, it means exactly what the old expression
meant, and the calibrated cases in `test_naming_a_credential_is_not_leaking_one.py` are untouched.
"""
from __future__ import annotations

import random
import re
import time

import pytest

from app import safety
from app.safety import assess_context, assess_draft
from app.schemas import PostContext

RULE = dict(safety.PII_PATTERNS)["credentials"]

# The expression as it was, kept here only as the definition of "the same meaning".
OLD = re.compile(
    r"\b(?:password|social security|ssn)\b"
    rf"|\bapi[ _-]?keys?\b[^.\n]{{0,60}}?(?:{safety._SECRET_SHAPED})"
    rf"|(?:{safety._SECRET_SHAPED})[^.\n]{{0,60}}?\bapi[ _-]?keys?\b", re.I)

HOSTILE = {
    "a base64 blob": "Look at this dump: " + "A" * 30_000,
    "a hash dump": "deadbeef" * 3_750,
    "a long url path": "https://example.com/" + "a/" * 15_000,
    "plus signs": "a+" * 15_000,
    "mentions without secrets": ("api key " + "A" * 5 + " ") * 2_000,
    "a blob then the mention": "A" * 30_000 + " is not an api key",
}


@pytest.mark.parametrize("label", sorted(HOSTILE))
def test_hostile_text_is_scanned_in_well_under_a_second(label):
    started = time.perf_counter()
    RULE.search(HOSTILE[label])
    assert time.perf_counter() - started < 0.5, label  # the blob took about twenty minutes before


def test_a_whole_post_context_at_its_limits_does_not_stall_the_engine():
    post = PostContext(author="stranger", text="Look at this dump: " + "A" * 29_000,
                       thread_text=["B" * 20_000, "c0ffee" * 3_000])
    started = time.perf_counter()
    assess_context(post)
    assess_draft("A" * 20_000)
    assert time.perf_counter() - started < 1.0


def test_it_means_what_the_old_expression_meant():
    """Same verdict as the old expression on 20,000 texts built from the pieces that matter."""
    rng = random.Random(20260920)
    pieces = ["api key", "API-Key", "apikeys", " api_key ", "sk-" + "a1B2" * 5, "ghp_" + "x9" * 10, "A" * 33, "f0" * 17,
              "deadbeef" * 4, "Zm9v" * 9 + "==", ".", "\n", " ", " is ", ": ", "the ", "my ", "x" * 10, "y" * 25, "q" * 61,
              "password", "ssn", "passwords", "a/b+c" * 8, "short1234", "-", "_", ",", "another "]
    for _ in range(20_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 9)))
        assert bool(RULE.search(text)) == bool(OLD.search(text)), text


# Shaped like keys and assembled from parts, so that no key-shaped literal sits in the source (the
# repository's release gate scans for them, and these are not secrets of anybody's).
_FAKE_PROVIDER_KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
_FAKE_FORGE_TOKEN = "gh" + "p_" + "1234567890abcdefghij1234567890abcdef"
_FAKE_CLOUD_KEY = "AKIA" + "-ABCDEFGHIJKLMNOP1234"


@pytest.mark.parametrize("text", [
    "api key: " + _FAKE_PROVIDER_KEY,
    _FAKE_FORGE_TOKEN + " is the api key",
    "here is my API key " + _FAKE_CLOUD_KEY,
    "my password is hunter2",
    "the api key is " + "Zm9vYmFy" * 6 + "==",
    "A" * 5_000 + " <- that is the api key",  # a very long token still counts
])
def test_real_disclosures_are_still_caught(text):
    assert RULE.search(text), text


@pytest.mark.parametrize("text", [
    "That's another API key, per-session token cost adds up fast",
    "It stops burning another API key per session",
    "rotate your api keys every quarter. " + "A" * 40,          # the token is in the NEXT sentence
    "A" * 40 + ". never share an api key",                       # and here in the previous one
    "api key" + " word" * 20 + " " + "A" * 40,                   # more than 60 characters away
])
def test_naming_a_credential_is_still_not_leaking_one(text):
    assert not RULE.search(text), text


def test_a_text_that_is_nothing_but_mentions_fails_closed_and_fast():
    text = "api key " * 5_000
    started = time.perf_counter()
    assert RULE.search(text), "hundreds of mentions is refused rather than examined one by one"
    assert time.perf_counter() - started < 0.5
