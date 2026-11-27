"""Adversarial cover for sign-up: addresses, the allowance key, races and the password policy.

Everything here is HTTP-level against the real app unless the invariant has no route
(the allowance key, which only billing reads).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from starling_server.auth import accounts, passwords
from starling_server.config import load_settings
from starling_server.security import ratelimit

from ..conftest import PUBLIC_URL, base_env
from .helpers import PASSWORD, account_row, context_value, login, outbox, page_token, post_form, signup


def signup_errors(response) -> dict:
    return context_value(response, "errors")


# --------------------------------------------------------------------------- addresses


def test_address_is_trimmed_and_case_folded_but_kept_for_display(app, client):
    assert signup(client, "  Ana.Smith@Example.COM  ").status_code == 303
    row = account_row(app, "ana.smith@example.com")
    assert row is not None
    assert row["email"] == "Ana.Smith@Example.COM"  # what the customer typed, for the greeting
    assert row["email_norm"] == "ana.smith@example.com"
    # Any casing and surrounding whitespace (a newline pasted from an email included)
    # reaches the same account.
    assert login(client, "  ANA.SMITH@EXAMPLE.com \r\n").status_code == 303


@pytest.mark.parametrize(
    "address",
    [
        "ana @example.com",  # non-breaking space
        "an a@example.com",  # inner space
        "ana@examрle.com",  # Cyrillic er: a lookalike domain
        "anä@example.com",
        "ana@example.com\r\nbcc: victim@example.com",  # header injection attempt
        "ana@@example.com",
        "ana@example",  # no public suffix
        "ana@-example.com",
        "ana@example-.com",
        "ana@example..com",
        "ana@example.c",  # one-letter TLD
        "@example.com",
        "ana@",
        ".ana@example.com",
        "ana.@example.com",
        "ana..smith@example.com",
        "a" * 65 + "@example.com",  # local part over 64
        "a" * 250 + "@example.com",  # address over 254
        "a" * 400 + "@example.com",  # longer than the form field cap
    ],
)
def test_refused_addresses_never_create_an_account(app, client, address):
    response = signup(client, address)
    assert response.status_code == 400, address
    assert "email" in signup_errors(response)
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_punycode_domains_are_accepted_as_ordinary_domains(app, client):
    # `xn--` labels are legitimate international domains; unicode in the address is not accepted,
    # so a lookalike can only reach us already punycoded, which is indistinguishable from a real one.
    assert signup(client, "ana@xn--80ak6aa92e.com").status_code == 303
    assert account_row(app, "ana@xn--80ak6aa92e.com") is not None


# --------------------------------------------------------------------------- one mailbox, one allowance


def test_one_mailbox_is_one_allowance_key():
    key = accounts.allowance_key
    assert key("ana+starling@example.com") == "ana@example.com"
    assert key("ana+a+b@example.com") == "ana@example.com"
    # Gmail (and googlemail) ignore dots and unify the domain; other providers do not.
    assert key("a.n.a+tag@gmail.com") == "ana@gmail.com"
    assert key("a.n.a@googlemail.com") == "ana@gmail.com"
    assert key("a.n.a@example.com") == "a.n.a@example.com"
    assert key("ana@example.com") == "ana@example.com"


def test_allowance_key_is_stored_with_the_account(app, client):
    assert signup(client, "Ana+Promo@Gmail.com").status_code == 303
    assert account_row(app, "ana+promo@gmail.com")["allowance_key"] == "ana@gmail.com"


def test_a_leading_plus_keeps_its_own_allowance_key(app, client):
    """A local part that is only a tag must not collapse to the bare domain.

    `+a@example.com` and `+b@example.com` are different mailboxes; if both normalised to
    `@example.com` one of them would inherit the other's deletion tombstone and silently
    lose its free allowance.
    """
    assert accounts.allowance_key("+a@example.com") != accounts.allowance_key("+b@example.com")
    assert signup(client, "+a@example.com").status_code == 303
    assert signup(client, "+b@example.com").status_code == 303
    with app.state.db.read() as conn:
        keys = [row[0] for row in conn.execute("SELECT allowance_key FROM accounts ORDER BY created_at")]
    assert len(set(keys)) == 2


# --------------------------------------------------------------------------- races and repeats


def test_two_simultaneous_signups_with_one_address_make_one_account(app, client):
    """Two browsers posting the same address at the same moment: one account, no 500."""
    browsers = [TestClient(app, base_url=PUBLIC_URL) for _ in range(2)]
    tokens = [page_token(browser) for browser in browsers]  # pre-session cookies in place

    def submit(index: int):
        return post_form(
            browsers[index], "/signup", {"email": "race@example.com", "password": PASSWORD}, token=tokens[index]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(submit, 0), pool.submit(submit, 1)]]

    assert [response.status_code for response in results] == [303, 303]
    assert [response.headers["location"] for response in results] == ["/verify-email/sent"] * 2
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    # The loser takes the "address already here, still unverified" branch and sends a second
    # verification link; that is the same mailbox, so it leaks nothing and both links work.
    templates = [message["template"] for message in outbox(client, "race@example.com")]
    assert templates == ["verify_email", "verify_email"]


def test_repeat_signup_for_an_unverified_address_resends_and_is_throttled(app, client, second_client, fake_clock):
    assert signup(client, "slow@example.com").status_code == 303
    assert signup(second_client, "slow@example.com").status_code == 303
    # The notice limit is one per hour per account, whatever the response says.
    assert signup(second_client, "slow@example.com").status_code == 303
    templates = [message["template"] for message in outbox(client, "slow@example.com")]
    assert templates == ["verify_email", "verify_email"]
    fake_clock.advance(3600)
    assert signup(second_client, "slow@example.com").status_code == 303
    assert len(outbox(client, "slow@example.com")) == 3


def test_signup_does_the_same_password_work_for_new_and_existing_addresses(app, client, second_client, monkeypatch):
    """No timing oracle: an existing address costs exactly one argon2 hash, like a new one."""
    assert signup(client, "known@example.com").status_code == 303
    calls: list[str] = []
    real_hash = passwords.PasswordHasher.hash

    def spy(self, password, salt=None):
        calls.append("hash")
        return real_hash(self, password)

    monkeypatch.setattr(passwords.PasswordHasher, "hash", spy)
    page_token(second_client)
    assert signup(second_client, "brand-new@example.com").status_code == 303
    new_calls = len(calls)
    assert signup(second_client, "known@example.com").status_code == 303
    assert new_calls == 1 and len(calls) == 2


# --------------------------------------------------------------------------- password policy


@pytest.mark.parametrize("password", ["a" * 9, "", "aaaaaaaaaa", "password123", "abababababab", "Zq7" * 43])
def test_passwords_outside_the_policy_are_refused(app, client, password):
    response = signup(client, "ana@example.com", password)
    assert response.status_code == 400
    assert "password" in signup_errors(response)
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "password",
    [
        "0123456789a",  # 11 plain characters
        "abcdefgh" * 16,  # exactly the 128-character maximum
        "Ελλάδα κόσμος 2026",  # non-ASCII
        "🐦🦅🦉🦆🦢🐧🕊🦜🦩🐓",  # 10 astral code points
        " leading and trailing spaces ",
    ],
)
def test_passwords_inside_the_policy_work_end_to_end(app, client, password):
    assert signup(client, "ana@example.com", password).status_code == 303
    assert login(client, "ana@example.com", password).status_code == 303


def test_an_enormous_password_is_refused_without_hashing_it(app, client, monkeypatch):
    """Oversized passwords must never reach argon2: that would be free CPU for an attacker."""
    calls: list[str] = []
    real_hash = passwords.PasswordHasher.hash
    monkeypatch.setattr(
        passwords.PasswordHasher, "hash", lambda self, password, salt=None: (calls.append("x"), real_hash(self, password))[1]
    )
    # Over the whole-form bound: a shaped 413, never a 500 and never a hash.
    huge = signup(client, "ana@example.com", "x" * 200_000)
    assert huge.status_code == 413
    # Over the field bound but inside the form: read as empty, so the policy refuses it.
    over_field = signup(client, "ana@example.com", "x" * 5_000)
    assert over_field.status_code == 400 and "password" in signup_errors(over_field)
    assert calls == []
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_an_oversized_password_at_sign_in_is_refused_against_the_dummy_hash(app, client, monkeypatch):
    assert signup(client, "ana@example.com", PASSWORD).status_code == 303
    seen: list[str] = []
    real_verify = passwords.PasswordHasher.verify
    monkeypatch.setattr(
        passwords.PasswordHasher,
        "verify",
        lambda self, hash, password: (seen.append(hash), real_verify(self, hash, password))[1],
    )
    dummy = passwords.dummy_hash(app.state.settings)
    assert login(client, "ana@example.com", "x" * 1_000).status_code == 400
    # 1000 characters is past MAX_LENGTH, so the stored hash is never touched: the dummy one is.
    assert seen == [dummy]


def test_the_email_address_cannot_be_the_password(app, client):
    response = signup(client, "longenough@example.com", "longenough@example.com")
    assert response.status_code == 400 and "password" in signup_errors(response)


# --------------------------------------------------------------------------- limits and blocklists


def test_signups_are_limited_per_network(app, monkeypatch):
    monkeypatch.setattr(ratelimit, "SIGNUP_PER_IP", ratelimit.Rule("signup_ip", 2, 3600))
    attacker = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.10", 5000))
    neighbour = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.77", 5000))
    elsewhere = TestClient(app, base_url=PUBLIC_URL, client=("198.51.100.5", 5000))
    assert signup(attacker, "a1@example.com").status_code == 303
    assert signup(attacker, "a2@example.com").status_code == 303
    blocked = signup(attacker, "a3@example.com")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert signup(neighbour, "a4@example.com").status_code == 429  # the whole /24 shares the budget
    assert signup(elsewhere, "a5@example.com").status_code == 303  # negative control
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3


def test_disposable_domains_and_their_subdomains_are_refused(tmp_path, web_dir):
    blocklist = tmp_path / "disposable.txt"
    blocklist.write_text("# comment\nmailinator.com\nTRASH.example.\n\n", encoding="utf-8")
    values = base_env(tmp_path / "data")
    values["STARLING_WEB_DIR"] = str(web_dir)
    values["DISPOSABLE_DOMAINS_FILE"] = str(blocklist)
    from starling_server.app import create_app

    with TestClient(create_app(load_settings(values)), base_url=PUBLIC_URL) as client:
        for address in ("ana@mailinator.com", "ana@MAILINATOR.com", "ana@mail.trash.example"):
            response = signup(client, address)
            assert response.status_code == 400, address
            assert "email" in signup_errors(response)
        assert signup(client, "ana@example.com").status_code == 303
