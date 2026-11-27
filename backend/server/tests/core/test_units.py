"""Server core building blocks without HTTP: addresses, passwords, TOTP, rate limits, cookies."""
from __future__ import annotations

import base64
import threading
import time

import pytest
from starlette.requests import Request
from starlette.responses import Response

from starling_server.auth import accounts, passwords, totp
from starling_server.auth.csrf import normalize_origin, origin_allowed, public_origin
from starling_server.config import load_settings
from starling_server.devices import connect
from starling_server.devices import tokens as device_tokens
from starling_server.security import ratelimit
from starling_server.security.http import safe_next
from starling_server.web import flash

from ..conftest import TEST_SECRET_KEY, base_env

ROTATED_KEY = base64.urlsafe_b64encode(b"rotated-server-key-for-tests-32b").decode().rstrip("=")


def make_request(peer: str = "198.51.100.4", headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (peer, 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
    )


# --------------------------------------------------------------------------- addresses


def test_email_validation_normalisation_and_allowance_key():
    assert accounts.validate_email("  Ana.Smith+news@Example.COM ") == "Ana.Smith+news@Example.COM"
    for bad in ["", "no-at-sign", "a@b", "a b@example.com", "a@example.com\r\nBcc: x@example.org",
                "a@-example.com", ".a@example.com", "a..b@example.com", "a@example.c", "a@[127.0.0.1]"]:
        with pytest.raises(accounts.EmailError):
            accounts.validate_email(bad)
    norm = accounts.normalize_email("  Ana.Smith+news@Example.COM ")
    assert norm == "ana.smith+news@example.com"
    assert accounts.allowance_key(norm) == "ana.smith@example.com"
    assert accounts.allowance_key("a.n.a+promo@googlemail.com") == "ana@gmail.com"
    assert accounts.allowance_key("ana@gmail.com") == accounts.allowance_key("a.na+x@gmail.com")
    # Negative control: dots only collapse for Gmail.
    assert accounts.allowance_key("a.na@example.com") != accounts.allowance_key("ana@example.com")
    assert accounts.mask_email("ana@example.com") == "a***@example.com"


def test_disposable_domain_blocklist(tmp_path):
    blocklist = tmp_path / "disposable.txt"
    blocklist.write_text("# temporary inboxes\nmailinator.com\n", encoding="utf-8")
    env = base_env(tmp_path / "data")
    env["DISPOSABLE_DOMAINS_FILE"] = str(blocklist)
    settings = load_settings(env)
    assert accounts.is_disposable(settings, "x@mailinator.com")
    assert accounts.is_disposable(settings, "x@eu.mailinator.com")
    assert not accounts.is_disposable(settings, "x@example.com")
    assert not accounts.is_disposable(load_settings(base_env(tmp_path / "data")), "x@mailinator.com")


# --------------------------------------------------------------------------- passwords


def test_password_policy():
    assert passwords.policy_errors("short")
    assert passwords.policy_errors("x" * 129)
    assert passwords.policy_errors("password123")
    assert passwords.policy_errors("abababababab")
    assert passwords.policy_errors("ana@example.com", "ana@example.com")
    assert passwords.policy_errors("anasmithxyz", "anasmithxyz@example.com")
    assert passwords.policy_errors("correct horse battery", "ana@example.com") == []


def test_hashing_rehash_and_dummy_verification(settings, monkeypatch):
    stored = passwords.hash_password(settings, "correct horse battery")
    assert stored.startswith("$argon2id$")
    assert passwords.verify_password(settings, stored, "correct horse battery") == passwords.VerifyResult(True, False)
    assert not passwords.verify_password(settings, stored, "wrong horse battery").ok
    other_parameters = passwords._hasher(2, 8192, 1).hash("correct horse battery")
    assert passwords.verify_password(settings, other_parameters, "correct horse battery") == passwords.VerifyResult(True, True)

    seen: list[str] = []
    real_verify = passwords.PasswordHasher.verify

    def spy(self, hash, password):
        seen.append(hash)
        return real_verify(self, hash, password)

    monkeypatch.setattr(passwords.PasswordHasher, "verify", spy)
    # No account: the same argon2 work runs against the dummy hash and never succeeds.
    assert passwords.verify_password(settings, None, "correct horse battery") == passwords.VerifyResult(False)
    assert seen == [passwords.dummy_hash(settings)]


def _peak_concurrency(settings, monkeypatch) -> int:
    active = peak = 0
    lock = threading.Lock()
    real_hash = passwords.PasswordHasher.hash

    def slow_hash(self, password, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        try:
            return real_hash(self, password, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(passwords.PasswordHasher, "hash", slow_hash)
    threads = [threading.Thread(target=passwords.hash_password, args=(settings, "correct horse battery")) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return peak


def test_argon2_work_is_bounded_by_the_semaphore(tmp_path, monkeypatch):
    env = base_env(tmp_path / "data")
    env["ARGON2_MAX_CONCURRENT"] = "2"
    assert _peak_concurrency(load_settings(env), monkeypatch) == 2
    env["ARGON2_MAX_CONCURRENT"] = "6"  # negative control: a larger limit lets more run at once
    assert _peak_concurrency(load_settings(env), monkeypatch) > 2


# --------------------------------------------------------------------------- TOTP

RFC6238_SHA1 = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def test_totp_matches_rfc6238_test_vectors():
    key = b"12345678901234567890"
    for at, expected in RFC6238_SHA1:
        assert totp.totp(key, at, digits=8) == expected
        assert totp.totp(key, at) == expected[-6:]


def test_totp_window_and_single_use(database):
    key = totp.decode_secret(totp.generate_secret())
    now = 1_789_000_000
    step = now // totp.STEP_S
    assert totp.matching_step(key, totp.hotp(key, step), now) == step
    assert totp.matching_step(key, totp.hotp(key, step - 1), now) == step - 1
    assert totp.matching_step(key, totp.hotp(key, step + 1), now) == step + 1
    assert totp.matching_step(key, totp.hotp(key, step + 3), now) is None
    assert totp.matching_step(key, "12345", now) is None
    assert database.write(lambda c: totp.claim_step(c, "account-a", step))
    assert not database.write(lambda c: totp.claim_step(c, "account-a", step))
    assert database.write(lambda c: totp.claim_step(c, "account-b", step))


def test_totp_secret_encryption_is_bound_to_key_and_account():
    key_a, key_b = b"a" * 32, b"b" * 32
    secret = totp.generate_secret()
    stored = totp.encrypt_secret(key_a, "account-1", secret)
    assert secret not in stored
    assert totp.decrypt_secret(key_a, "account-1", stored) == secret
    assert totp.decrypt_secret(key_b, "account-1", stored) is None
    assert totp.decrypt_secret(key_a, "account-2", stored) is None
    assert totp.decrypt_with_any((key_b, key_a), "account-1", stored) == (secret, True)
    assert totp.decrypt_with_any((key_a,), "account-1", stored) == (secret, False)
    assert totp.decrypt_with_any((key_b,), "account-1", stored) == (None, False)


# --------------------------------------------------------------------------- rate limits


def test_fixed_window_rate_limit(database):
    rule = ratelimit.Rule("unit", 3, 60)
    now = 1_789_000_020
    decisions = [database.write(lambda c: ratelimit.hit(c, rule, "k", now)) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after_s == 60
    assert database.write(lambda c: ratelimit.hit(c, rule, "other-key", now)).allowed
    assert database.write(lambda c: ratelimit.hit(c, rule, "k", now + 60)).allowed


def test_login_delay_is_progressive_not_a_lockout(database):
    key = ratelimit.email_key("ana@example.com")
    now = 1_789_000_000
    for _ in range(ratelimit.LOGIN_FREE_FAILURES):
        assert database.write(lambda c: ratelimit.login_gate(c, key, now)).allowed
        database.write(lambda c: ratelimit.record_login_failure(c, key, now))
    refused = database.write(lambda c: ratelimit.login_gate(c, key, now + 1))
    assert not refused.allowed and refused.retry_after_s == 1
    assert database.write(lambda c: ratelimit.login_gate(c, key, now + 2)).allowed
    # The slot is claimed: a parallel guess at the same moment waits.
    assert not database.write(lambda c: ratelimit.login_gate(c, key, now + 2)).allowed
    assert [ratelimit.login_delay_s(n) for n in (4, 5, 6, 7, 40)] == [0, 2, 4, 8, ratelimit.LOGIN_MAX_DELAY_S]
    database.write(lambda c: ratelimit.record_login_success(c, key))
    assert database.write(lambda c: ratelimit.login_gate(c, key, now + 3)).allowed


def test_ip_prefixes_and_forwarded_for_only_behind_trusted_proxies(tmp_path):
    assert ratelimit.ip_prefix("203.0.113.77") == "203.0.113.0/24"
    assert ratelimit.ip_prefix("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert ratelimit.ip_prefix("::ffff:203.0.113.9") == "203.0.113.0/24"
    untrusted = load_settings(base_env(tmp_path / "data"))
    assert ratelimit.client_ip(make_request("198.51.100.4", {"X-Forwarded-For": "203.0.113.50"}), untrusted) == "198.51.100.4"
    env = base_env(tmp_path / "data")
    env["STARLING_TRUSTED_PROXIES"] = "10.0.0.0/8"
    trusted = load_settings(env)
    via_proxy = make_request("10.1.2.3", {"X-Forwarded-For": "192.0.2.1, 203.0.113.50, 10.9.9.9"})
    assert ratelimit.client_ip(via_proxy, trusted) == "203.0.113.50"
    assert ratelimit.client_ip(make_request("198.51.100.4", {"X-Forwarded-For": "203.0.113.50"}), trusted) == "198.51.100.4"


# --------------------------------------------------------------------------- request helpers


def test_safe_next_allows_only_same_origin_paths():
    assert safe_next("/account/devices?x=1") == "/account/devices?x=1"
    for bad in ["https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "account",
                "/ok\r\nSet-Cookie: x=1", ""]:
        assert safe_next(bad, "/fallback") == "/fallback"


def test_extension_versions_and_device_labels():
    assert device_tokens.version_ok("1.0.0", "1.0.0")
    assert device_tokens.version_ok("1.2", "1.0.0")
    assert device_tokens.version_ok("2.0.0-beta.1", "1.9.9")
    assert not device_tokens.version_ok("0.9.9", "1.0.0")
    assert not device_tokens.version_ok(None, "1.0.0")
    assert not device_tokens.version_ok("latest", "1.0.0")
    assert connect.sanitize_label("  My‮Laptop\n\t ") == "My Laptop"
    assert connect.sanitize_label("x" * 100) == "x" * 60
    assert connect.sanitize_label("\x00\x01") == "Chrome"


def test_origin_rules(settings):
    assert public_origin(settings) == "http://127.0.0.1:8765"
    assert normalize_origin("HTTP://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert normalize_origin("https://example.com:443") == "https://example.com"
    assert normalize_origin("null") is None
    assert origin_allowed(make_request(headers={"Origin": "http://127.0.0.1:8765"}), settings)
    assert not origin_allowed(make_request(headers={"Origin": "https://evil.example"}), settings)
    assert not origin_allowed(make_request(headers={"Origin": "null"}), settings)
    assert not origin_allowed(make_request(), settings)
    assert origin_allowed(make_request(headers={"Sec-Fetch-Site": "same-origin"}), settings)
    assert not origin_allowed(make_request(headers={"Sec-Fetch-Site": "same-site"}), settings)


def test_signed_cookies_reject_tampering_and_follow_key_rotation(tmp_path):
    old = load_settings(base_env(tmp_path / "data"))
    response = Response()
    flash.set_flash(response, old, "success", "Saved.")
    value = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    name = flash.flash_cookie_name(old)

    def with_cookie(cookie_value: str) -> Request:
        return make_request(headers={"Cookie": f"{name}={cookie_value}"})

    assert flash.read_flash(with_cookie(value), old) == [{"level": "success", "text": "Saved."}]
    data, _, signature = value.partition(".")
    forged = data + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    assert flash.read_flash(with_cookie(forged), old) == []

    env = base_env(tmp_path / "data")
    env["STARLING_SECRET_KEY"] = ROTATED_KEY
    env["STARLING_SECRET_KEY_PREVIOUS"] = TEST_SECRET_KEY
    assert flash.read_flash(with_cookie(value), load_settings(env)) == [{"level": "success", "text": "Saved."}]
    env.pop("STARLING_SECRET_KEY_PREVIOUS")
    assert flash.read_flash(with_cookie(value), load_settings(env)) == []
