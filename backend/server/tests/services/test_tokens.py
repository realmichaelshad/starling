"""Services tokens (CONTRACTS 4.3): tampering, expiry, wrong keys and subject swaps are refused."""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import pytest

from starling_server.services import tokens

KEY = b"k" * 32
OTHER_KEY = b"q" * 32
NOW = 1_800_000_000
TENANT_A = str(uuid.UUID("3f2a9c4e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"))
TENANT_B = str(uuid.UUID("c0ffee00-1234-4abc-9def-0123456789ab"))


def _replace(token: str, index: int, value: str) -> str:
    parts = token.split(".")
    parts[index] = value
    return ".".join(parts)


def test_engine_token_has_the_contract_format_and_verifies():
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    version, subject, expires, nonce, signature = token.split(".")
    assert (version, subject, int(expires)) == ("svc1", f"t-{TENANT_A}", NOW + 24 * 3600)
    assert len(base64.urlsafe_b64decode(nonce + "==")) == 16 and "=" not in nonce
    expected = hmac.new(KEY, f"svc1.{subject}.{expires}.{nonce}".encode(), hashlib.sha256).digest()
    assert signature == base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    verified = tokens.verify(KEY, token, NOW)
    assert verified.kind == "tenant" and verified.tenant_id == TENANT_A and not verified.is_admin
    assert tokens.mint_engine_token(KEY, TENANT_A, NOW) != token  # fresh nonce every time


def test_admin_token_lives_five_minutes():
    token = tokens.mint_admin_token(KEY, NOW)
    assert token.split(".")[2] == str(NOW + 300)
    subject = tokens.verify(KEY, token, NOW + 299)
    assert subject.is_admin and subject.tenant_id is None
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, token, NOW + 300)


def test_expired_engine_token_is_refused():
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    assert tokens.verify(KEY, token, NOW + 24 * 3600 - 1).tenant_id == TENANT_A  # negative control
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, token, NOW + 24 * 3600)


def test_wrong_key_is_refused():
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    assert tokens.verify(KEY, token, NOW)  # negative control
    with pytest.raises(tokens.TokenError):
        tokens.verify(OTHER_KEY, token, NOW)


@pytest.mark.parametrize("index", [2, 3, 4])
def test_tampering_with_any_signed_part_is_refused(index):
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    original = token.split(".")[index]
    changed = ("A" if original[0] != "A" else "B") + original[1:]
    if index == 2:
        changed = str(int(original) + 3600)
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, _replace(token, index, changed), NOW)


def test_subject_swap_is_refused():
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, _replace(token, 1, f"t-{TENANT_B}"), NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, _replace(token, 1, "admin"), NOW)
    assert tokens.verify_engine_token(KEY, token, TENANT_A, NOW).tenant_id == TENANT_A  # negative control
    with pytest.raises(tokens.TokenError):
        tokens.verify_engine_token(KEY, token, TENANT_B, NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify_engine_token(KEY, tokens.mint_admin_token(KEY, NOW), TENANT_A, NOW)


def test_unknown_and_non_canonical_subjects_are_refused_even_when_signed():
    assert tokens.verify(KEY, tokens._mint(KEY, "admin", NOW + 60), NOW).is_admin  # negative control
    for subject in ("root", "t-not-a-uuid", f"t-{TENANT_A.upper()}", f"T-{TENANT_A}"):
        with pytest.raises(tokens.TokenError):
            tokens.verify(KEY, tokens._mint(KEY, subject, NOW + 60), NOW)


def test_lifetimes_longer_than_the_contract_are_refused():
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, tokens._mint(KEY, f"t-{TENANT_A}", NOW + 24 * 3600 + 3600), NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, tokens._mint(KEY, "admin", NOW + 3600), NOW)
    with pytest.raises(tokens.TokenError):
        tokens.mint_admin_token(KEY, NOW, ttl=3600)
    with pytest.raises(tokens.TokenError):
        tokens.mint_engine_token(KEY, TENANT_A, NOW, ttl=0)


@pytest.mark.parametrize(
    "value",
    ["", "svc1", "svc1.admin.1.2", "svc2.admin.1800000100.AAAAAAAAAAAAAAAAAAAAAA.sig", "é" * 20, "x" * 300, None, 12],
)
def test_malformed_tokens_are_refused(value):
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, value, NOW)  # type: ignore[arg-type]


def test_extra_segments_and_bad_tenants_are_refused():
    token = tokens.mint_engine_token(KEY, TENANT_A, NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify(KEY, token + ".extra", NOW)
    with pytest.raises(tokens.TokenError):
        tokens.mint_engine_token(KEY, "not-a-uuid", NOW)


def test_missing_or_short_keys_are_refused():
    with pytest.raises(tokens.TokenError):
        tokens.mint_engine_token(b"short", TENANT_A, NOW)
    with pytest.raises(tokens.TokenError):
        tokens.verify(b"", tokens.mint_engine_token(KEY, TENANT_A, NOW), NOW)


def test_signature_comparison_is_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(tokens.hmac, "compare_digest", spy)
    tokens.verify(KEY, tokens.mint_engine_token(KEY, TENANT_A, NOW), NOW)
    assert calls
