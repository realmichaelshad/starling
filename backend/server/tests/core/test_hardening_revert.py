"""The email-change revert link, and the device limit where devices are actually created.

Both found by the security review.

THE REVERT LINK is the one control against a hijacked session changing an account's address: the
old address gets a link, valid for a week, that puts the address back, sets a new password and signs
every device out. It restores the OLD address, and that address became free the moment the change
was confirmed. An attacker who registered a throwaway account on it (no confirmation needed to hold
an address) made the restore impossible; the link then reported plain success, the account stayed
on the attacker's address, and one ordinary password reset gave it back to the attacker for good.
"""
from __future__ import annotations

from .helpers import (
    PASSWORD, account_row, approve_connect, connect_device, connect_params, context_value, create_user, link_token, outbox, pkce_pair,
    post_form, redeem, signup,
)

OLD, ATTACKER = "ana@example.com", "attacker@evil.example"
NEW_PASSWORD = "a wholly different passphrase 42"


def change_email_as_the_attacker(client) -> str:
    """The victim is signed in; the 'attacker' drives the same session. Returns the revert token."""
    create_user(client, OLD)
    changed = post_form(client, "/account/security/email", {"new_email": ATTACKER, "email_password": PASSWORD})
    assert changed.status_code in (200, 303), changed.text
    revert = link_token(client, OLD, "/reset-password")  # sent to the OLD address when the change is asked for
    confirm = link_token(client, ATTACKER, "/verify-email")
    assert post_form(client, "/verify-email", {"token": confirm}).status_code == 303
    assert account_row(client.app, ATTACKER) is not None and account_row(client.app, OLD) is None, "the address was released"
    return revert


def test_nobody_can_register_an_address_while_its_owner_can_still_take_it_back(app, client, second_client, fake_state):
    revert = change_email_as_the_attacker(client)

    # The attacker parks a throwaway account on the old address to block the undo.
    sent_before = len([m for m in outbox(second_client, OLD) if m["template"] == "verify_email"])
    assert signup(second_client, OLD).status_code == 303, "answered like any other sign-up: nothing tells them apart"
    with app.state.db.read() as conn:
        held = conn.execute("SELECT COUNT(*) FROM accounts WHERE email_norm = ?", (OLD,)).fetchone()[0]
    assert held == 0, "but no account was created on it"
    sent_after = len([m for m in outbox(second_client, OLD) if m["template"] == "verify_email"])
    assert sent_after == sent_before, "and no confirmation link went out for one"

    done = post_form(client, "/reset-password", {"token": revert, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert done.status_code == 303
    with app.state.db.read() as conn:
        row = conn.execute("SELECT email_norm FROM accounts WHERE email_norm IN (?, ?)", (OLD, ATTACKER)).fetchall()
    assert [r["email_norm"] for r in row] == [OLD], "the address is back where it belongs"


def test_an_unconfirmed_account_parked_on_the_address_gives_it_up(app, client, second_client, fake_state):
    """Belt and braces: an account that got onto the address anyway never proved it receives its mail."""
    revert = change_email_as_the_attacker(client)
    from starling_server.auth import accounts

    squatter = app.state.db.write(lambda conn: accounts.insert_account(
        conn, email=OLD, password_hash="$argon2id$placeholder", ip_prefix=None, now=1_800_000_000).id)

    done = post_form(client, "/reset-password", {"token": revert, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert done.status_code == 303
    with app.state.db.read() as conn:
        victim = conn.execute("SELECT id FROM accounts WHERE email_norm = ?", (OLD,)).fetchone()
        parked = conn.execute("SELECT email_norm, status, password_hash FROM accounts WHERE id = ?", (squatter,)).fetchone()
    assert victim is not None and victim["id"] != squatter
    assert parked["email_norm"].endswith("@address.invalid") and parked["status"] == "suspended"
    assert parked["password_hash"] == "!released", "and it can never be signed in to again"


def test_a_confirmed_account_on_the_address_is_never_touched_and_the_failure_is_said_out_loud(app, client, fake_state):
    revert = change_email_as_the_attacker(client)
    from starling_server.auth import accounts

    def confirmed(conn):
        account = accounts.insert_account(conn, email=OLD, password_hash="$argon2id$placeholder", ip_prefix=None, now=1_800_000_000)
        accounts.mark_verified(conn, account.id, 1_800_000_001)
        return account.id

    other = app.state.db.write(confirmed)
    done = post_form(client, "/reset-password", {"token": revert, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert done.status_code == 303
    page = client.get("/login").text
    assert "could NOT be restored" in page and "Contact support" in page
    assert "Your password changed and every device was signed out. Sign in" not in page, "never reported as plain success"
    with app.state.db.read() as conn:
        untouched = conn.execute("SELECT email_norm, status FROM accounts WHERE id = ?", (other,)).fetchone()
    assert (untouched["email_norm"], untouched["status"]) == (OLD, "active")


def test_once_the_week_is_over_the_address_is_free_again(app, client, second_client, fake_state, fake_clock):
    change_email_as_the_attacker(client)
    fake_clock.advance(8 * 86_400)
    assert signup(second_client, OLD).status_code == 303
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE email_norm = ?", (OLD,)).fetchone()[0] == 1


# --------------------------------------------------------------------------- the device limit


def test_approving_many_requests_first_does_not_get_past_the_device_limit(app, client, fake_state):
    """No device exists until a code is redeemed, so thirty approvals each saw an account under its limit."""
    import dataclasses

    app.state.settings = dataclasses.replace(app.state.settings, max_devices_per_account=3)
    create_user(client, OLD)
    pending = []
    for _ in range(6):
        verifier, challenge = pkce_pair()
        code, _ = approve_connect(client, connect_params(challenge))
        pending.append((code, verifier))

    answers = [redeem(client, code, verifier) for code, verifier in pending]
    assert [a.status_code for a in answers] == [200, 200, 200, 400, 400, 400]
    assert "Disconnect one on the Devices page" in answers[3].json()["error"]["message"]
    assert len(context_value(client.get("/account/devices"), "devices")) == 3

    # A place freed on the Devices page can be used again.
    first = answers[0].json()["device_id"]
    assert post_form(client, f"/account/devices/{first}/revoke", {}).status_code == 303
    assert connect_device(client)["device_id"]
