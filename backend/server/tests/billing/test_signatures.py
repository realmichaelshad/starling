"""Webhook signature verification (CONTRACTS section 8, security invariant 10)."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from starling_server.billing.signatures import SignatureError, compute_signature, sign_header, verify

SECRET = "whsec_" + "a" * 32
OLD = "whsec_" + "b" * 32
OTHER = "whsec_" + "c" * 32
BODY = b'{"id":"evt_00000001","type":"invoice.paid","livemode":false}'
NOW = 1_790_000_000


def test_signature_follows_the_stripe_scheme():
    expected = hmac.new(SECRET.encode(), f"{NOW}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert compute_signature(BODY, SECRET, NOW) == expected
    assert verify(BODY, sign_header(BODY, SECRET, NOW), [SECRET], now=NOW) == NOW


def test_wrong_secret_is_rejected():
    with pytest.raises(SignatureError):
        verify(BODY, sign_header(BODY, OTHER, NOW), [SECRET], now=NOW)


def test_tampered_body_is_rejected():
    header = sign_header(BODY, SECRET, NOW)
    assert verify(BODY, header, [SECRET], now=NOW) == NOW  # control
    with pytest.raises(SignatureError):
        verify(BODY.replace(b"invoice.paid", b"invoice.void"), header, [SECRET], now=NOW)


def test_timestamp_cannot_be_changed_without_the_signature():
    signature = compute_signature(BODY, SECRET, NOW)
    with pytest.raises(SignatureError):
        verify(BODY, f"t={NOW + 1},v1={signature}", [SECRET], now=NOW)


def test_stale_and_future_timestamps_are_rejected():
    assert verify(BODY, sign_header(BODY, SECRET, NOW - 300), [SECRET], now=NOW) == NOW - 300
    assert verify(BODY, sign_header(BODY, SECRET, NOW + 300), [SECRET], now=NOW) == NOW + 300
    with pytest.raises(SignatureError, match="too old"):
        verify(BODY, sign_header(BODY, SECRET, NOW - 301), [SECRET], now=NOW)
    with pytest.raises(SignatureError, match="future"):
        verify(BODY, sign_header(BODY, SECRET, NOW + 301), [SECRET], now=NOW)


def test_two_v1_signatures_while_a_secret_is_rolled():
    header = sign_header(BODY, [OLD, SECRET], NOW)  # one v1 per active secret
    assert header.count("v1=") == 2
    assert verify(BODY, header, [SECRET], now=NOW) == NOW
    assert verify(BODY, header, [OLD], now=NOW) == NOW
    with pytest.raises(SignatureError):
        verify(BODY, header, [OTHER], now=NOW)


def test_previous_secret_is_accepted_only_when_configured():
    header = sign_header(BODY, OLD, NOW)
    assert verify(BODY, header, [SECRET, OLD], now=NOW) == NOW
    with pytest.raises(SignatureError):
        verify(BODY, header, [SECRET], now=NOW)


def test_v0_signatures_are_ignored():
    downgrade = compute_signature(BODY, SECRET, NOW)
    with pytest.raises(SignatureError):
        verify(BODY, f"t={NOW},v0={downgrade}", [SECRET], now=NOW)


@pytest.mark.parametrize(
    "header",
    [None, "", "v1=abc", f"t=soon,v1={'0' * 64}", f"t={NOW}", f"t=1,t=2,v1={'0' * 64}", "t=1," + "v1=a," * 17, "x" * 9000],
)
def test_malformed_headers_are_rejected(header):
    with pytest.raises(SignatureError):
        verify(BODY, header, [SECRET], now=NOW)


@pytest.mark.parametrize("tolerance", [0, -1])
def test_zero_tolerance_is_refused(tolerance):
    with pytest.raises(ValueError):
        verify(BODY, sign_header(BODY, SECRET, NOW), [SECRET], tolerance_s=tolerance, now=NOW)


def test_missing_secret_is_a_configuration_error():
    header = sign_header(BODY, SECRET, NOW)
    for secrets in ([], [""]):
        with pytest.raises(ValueError):
            verify(BODY, header, secrets, now=NOW)
