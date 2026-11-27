"""Account flows behind the website routes.

Each flow validates input, does expensive password work outside the database writer, then makes
all related changes (account rows, tokens, sessions, devices, queued email) in one writer
transaction. Queued email is handed to delivery only after that transaction commits.

Responses must not reveal whether an address has an account: sign-up, sign-in and password reset
do the same visible thing either way, and the password work runs against a dummy hash when there
is no account.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..devices import tokens as device_tokens
from ..email import sender
from ..email import templates as mail
from ..security import clock, ratelimit
from ..security.crypto import derive_key, equals, random_token, sha256_hex, sign, signing_keys
from . import accounts, email_tokens, identities, passwords, sessions
from .google import GoogleUser

log = logging.getLogger(__name__)

# How long after signing in a Google-only account may do what a password would otherwise guard
# (change the email address, set a password, delete the account). Older than this, the page asks
# the person to confirm with Google again, which starts a fresh sign-in.
RECENT_SIGN_IN_S = 10 * 60
REAUTH_NEEDED = "For your security, confirm it is you with Google first."


def verify_link(settings: Settings, token: str) -> str:
    return f"{settings.public_url}/verify-email?token={token}"


def reset_link(settings: Settings, token: str) -> str:
    return f"{settings.public_url}/reset-password?token={token}"


def _minutes(seconds: int) -> int:
    return max(1, -(-seconds // 60))


# --------------------------------------------------------------------------- sign-up codes


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_digest(secret_key: bytes, code: str) -> str:
    """What `email_tokens.code_hash` holds: an HMAC, so a copy of the database alone cannot reveal a code."""
    return sign(derive_key(secret_key, "signup-code"), code)


def code_matches(settings: Settings, code: str, stored: str | None) -> bool:
    return bool(stored) and any(equals(code_digest(key, code), stored) for key in signing_keys(settings))


# --------------------------------------------------------------------------- sign-up


@dataclass
class SignupResult:
    email: str
    errors: dict[str, str] = field(default_factory=dict)
    retry_after_s: int | None = None
    # A random value for this browser's notice cookie, made for EVERY sign-up, new address or not, so
    # the response looks the same either way. Its hash ties the email's code to this browser.
    browser: str = ""


async def signup(db: Database, settings: Settings, *, email_raw: object, password: str, ip_prefix: str) -> SignupResult:
    errors: dict[str, str] = {}
    email = ""
    try:
        email = accounts.validate_email(email_raw)
    except accounts.EmailError as exc:
        errors["email"] = str(exc)
    email_norm = accounts.normalize_email(email) if email else ""
    if email_norm and accounts.is_disposable(settings, email_norm):
        errors["email"] = "Use a permanent email address. Temporary inbox services cannot be used."
    policy = passwords.policy_errors(password, email or None)
    if policy:
        errors["password"] = policy[0]
    if errors:
        return SignupResult(email, errors)

    limit = await ratelimit.check(db, [(ratelimit.SIGNUP_PER_IP, ratelimit.ip_key(ip_prefix))])
    if not limit.allowed:
        return SignupResult(email, {"form": "Too many sign-ups from your network. Try again later."}, limit.retry_after_s)
    if settings.signup_global_per_hour > 0:
        # THE WHOLE SITE, NOT ONE NETWORK. The rule above stops one network; a flood spread over
        # thousands of addresses passes it everywhere at once, and every sign-up it gets through
        # costs an argon2 hash, an account row, a tenant number and an outbound email from the
        # owner's sending domain. Checked after the per-network rule, so one noisy network
        # cannot use this budget up for everybody else.
        overall = await ratelimit.check(
            db,
            [(ratelimit.Rule(ratelimit.SIGNUP_GLOBAL_BUCKET, settings.signup_global_per_hour, 3600), ratelimit.SIGNUP_GLOBAL_KEY)],
        )
        if not overall.allowed:
            log.warning("sign-up refused: the site-wide ceiling of %d an hour is reached", settings.signup_global_per_hour)
            return SignupResult(
                email, {"form": "Sign-ups are busy right now. Please try again in a little while."}, overall.retry_after_s
            )

    # Hash first, whatever happens next, so a taken address costs the same time as a new one.
    password_hash = await passwords.hash_password_async(settings, password)
    now = clock.now()
    browser = random_token()
    code = new_code()
    tie = {"browser_hash": sha256_hex(browser), "code_hash": code_digest(settings.secret_key, code)}

    def transaction(conn: sqlite3.Connection) -> list[sender.QueuedEmail]:
        existing = accounts.get_by_email(conn, email_norm)
        if existing is None and email_tokens.address_is_held_for_a_revert(conn, email_norm, now):
            # The address was just released by an email change that its owner can still undo. Answer
            # exactly as for any other sign-up (nothing here tells the two apart) and create nothing.
            log.info("signup refused: the address is held for an email-change revert")
            return []
        if existing is None:
            account = accounts.insert_account(conn, email=email, password_hash=password_hash, ip_prefix=ip_prefix, now=now)
            token = email_tokens.issue(conn, account.id, "verify", email_tokens.VERIFY_TTL_S, now, **tie)
            log.info("signup account=%s tenant_seq=%d", account.id, account.tenant_seq)
            content = mail.verify_email(settings, verify_link(settings, token), code)
            return [sender.queue_content(conn, settings, account.email, content, now)]
        if not existing.can_sign_in:
            return []
        notice = ratelimit.hit(conn, ratelimit.SIGNUP_NOTICE_PER_ACCOUNT, ratelimit.account_key(existing.id), now)
        if not notice.allowed:
            return []
        if existing.email_verified:
            content = mail.signup_existing(settings, with_google=not existing.has_password)
            return [sender.queue_content(conn, settings, existing.email, content, now)]
        # The address has an account that never confirmed it. Whoever confirms from THIS browser, with
        # the code or the link from the inbox, takes the account over with the password typed here:
        # the first account may have been parked on the address by someone else, waiting for its
        # owner (`confirm_signup_code`). The link alone, opened in another browser, only confirms.
        token = email_tokens.issue(
            conn, existing.id, "verify", email_tokens.VERIFY_TTL_S, now, new_password_hash=password_hash, **tie
        )
        content = mail.verify_email(settings, verify_link(settings, token), code)
        return [sender.queue_content(conn, settings, existing.email, content, now)]

    queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    return SignupResult(email, browser=browser)


@dataclass
class CodeResult:
    # "ok" | "wrong" | "format" | "used_up" | "limited"; "waiting" from `continue_signup`
    status: str
    retry_after_s: int | None = None
    session_id: str | None = None
    session: sessions.WebSession | None = None
    account: accounts.Account | None = None


def _finish_signup(conn: sqlite3.Connection, account: accounts.Account, row: email_tokens.CodeToken, now: int) -> accounts.Account:
    """Both halves of the proof are in: this browser signed up, and its person reads the inbox."""
    if row.new_password_hash:
        # A second sign-up for an address whose first account was never confirmed: the account is
        # this person's now, with the password they typed, and whatever the first sign-up left
        # behind (sessions, connected browsers, links) stops working.
        accounts.set_password_hash(conn, account.id, row.new_password_hash, now)
        accounts.bump_auth_epoch(conn, account.id)
        sessions.revoke_all(conn, account.id, now)
        device_tokens.revoke_all(conn, account.id, now, "signup_confirmed_elsewhere")
        email_tokens.invalidate(conn, account.id, "reset", now, plain_resets_only=True)
    accounts.mark_verified(conn, account.id, now)
    email_tokens.invalidate(conn, account.id, "verify", now)
    current = accounts.get_by_id(conn, account.id)
    assert current is not None
    return current


async def confirm_signup_code(
    db: Database,
    settings: Settings,
    *,
    browser: str | None,
    account_id: str | None,
    code_raw: str,
    ip_prefix: str,
    user_agent: str | None,
    old_session_id: str | None,
) -> CodeResult:
    """The 6-digit code from the verification email, typed where the person signed up.

    Signed out, the code counts only in the browser the sign-up came from (`browser`, from its notice
    cookie), and that browser is signed in. Signed in (a resend from the account page), it confirms
    the signed-in account. Five wrong codes and that code is finished; the link still works.
    """
    code = "".join(ch for ch in code_raw if ch.isdigit())
    if len(code) != 6:
        return CodeResult("format")
    limit = await ratelimit.check(db, [(ratelimit.TOKEN_FORM_PER_IP, ratelimit.ip_key(ip_prefix))])
    if not limit.allowed:
        return CodeResult("limited", limit.retry_after_s)
    now = clock.now()

    def transaction(conn: sqlite3.Connection) -> CodeResult:
        if account_id is not None:
            row = email_tokens.code_token_for_account(conn, account_id, now)
        elif browser:
            row = email_tokens.code_token_for_browser(conn, sha256_hex(browser), now)
        else:
            row = None
        if row is None:
            return CodeResult("wrong")
        if row.code_attempts >= email_tokens.CODE_ATTEMPTS:
            return CodeResult("used_up")
        if not code_matches(settings, code, row.code_hash):
            attempts = email_tokens.count_code_attempt(conn, row.id)
            return CodeResult("used_up" if attempts >= email_tokens.CODE_ATTEMPTS else "wrong")
        account = accounts.get_by_id(conn, row.account_id)
        if account is None or not account.can_sign_in or not email_tokens.use_code(conn, row.id, now):
            return CodeResult("wrong")
        account = _finish_signup(conn, account, row, now)
        if account_id is not None:
            return CodeResult("ok", account=account)
        session_id, session = _start_session(
            conn, account, now=now, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
        )
        return CodeResult("ok", session_id=session_id, session=session, account=account)

    result = await db.write_async(transaction)
    if result.status == "ok" and result.account is not None:
        log.info("signup confirmed with a code account=%s signed_in=%s", result.account.id, result.session_id is not None)
    return result


async def resend_signup_email(db: Database, settings: Settings, *, browser: str) -> str:
    """"Send it again" on the check-your-email page, signed out. Returns "sent" or "limited".

    The spacing rule is keyed by the browser, not the account, so the answer is the same whether or
    not the sign-up found an existing confirmed account (for which there is nothing to resend).
    """
    browser_hash = sha256_hex(browser)
    now = clock.now()
    code = new_code()

    def transaction(conn: sqlite3.Connection):
        if not ratelimit.hit(conn, ratelimit.VERIFY_RESEND_SPACING, "browser:" + browser_hash, now).allowed:
            return "limited", []
        row = email_tokens.code_token_for_browser(conn, browser_hash, now)
        account = accounts.get_by_id(conn, row.account_id) if row is not None else None
        if row is None or account is None or not account.can_sign_in:
            return "sent", []
        if not ratelimit.hit(conn, ratelimit.VERIFY_RESEND_PER_ACCOUNT, ratelimit.account_key(account.id), now).allowed:
            return "sent", []
        email_tokens.invalidate(conn, account.id, "verify", now)
        token = email_tokens.issue(
            conn, account.id, "verify", email_tokens.VERIFY_TTL_S, now,
            browser_hash=browser_hash, code_hash=code_digest(settings.secret_key, code),
            new_password_hash=row.new_password_hash,
        )
        content = mail.verify_email(settings, verify_link(settings, token), code)
        return "sent", [sender.queue_content(conn, settings, account.email, content, now)]

    outcome, queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    return outcome


def _carries_on(account: accounts.Account | None, row: email_tokens.CodeToken) -> bool:
    """May the browser that signed up sign itself in, now that its link was opened in another one?

    That browser holds the sign-up's notice cookie and typed the account's password when it signed
    up, so signing it in proves no more than the sign-in page would. Only when the link opened
    elsewhere was the one from THIS sign-up's email (an account parked on someone's address does not
    ride on its owner's click), never after the password changed, and never for a second sign-up
    whose password waits on the token: that one takes the account over, which only the code or the
    link, in its own browser, may do.
    """
    return (
        account is not None and account.can_sign_in and account.email_verified
        and row.used_at is not None and not row.new_password_hash
        and account.password_changed_at <= row.created_at
    )


def signup_state(db: Database, *, browser: str) -> str:
    """"confirmed" once the sign-up this browser made can carry on by itself, else "waiting".

    Asked every few seconds by the check-your-email page. An address that already had a confirmed
    account issued no code to this browser, so that sign-up waits like any other: nothing here says
    whether an address has an account.
    """
    now = clock.now()
    with db.read() as conn:
        row = email_tokens.code_token_for_browser(conn, sha256_hex(browser), now)
        if row is None:
            return "waiting"
        return "confirmed" if _carries_on(accounts.get_by_id(conn, row.account_id), row) else "waiting"


async def continue_signup(
    db: Database, *, browser: str, ip_prefix: str, user_agent: str | None, old_session_id: str | None,
) -> CodeResult:
    """Sign in the browser that signed up, once its link was opened in another browser (`_carries_on`)."""
    now = clock.now()

    def transaction(conn: sqlite3.Connection) -> CodeResult:
        row = email_tokens.code_token_for_browser(conn, sha256_hex(browser), now)
        account = accounts.get_by_id(conn, row.account_id) if row is not None else None
        if row is None or account is None or not _carries_on(account, row) or not email_tokens.use_code(conn, row.id, now):
            return CodeResult("waiting")
        email_tokens.invalidate(conn, account.id, "verify", now)
        session_id, session = _start_session(
            conn, account, now=now, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
        )
        return CodeResult("ok", session_id=session_id, session=session, account=account)

    result = await db.write_async(transaction)
    if result.status == "ok" and result.account is not None:
        log.info("signup carried on in the browser that signed up account=%s", result.account.id)
    return result


# --------------------------------------------------------------------------- sign-in


@dataclass
class LoginResult:
    status: str  # "ok" | "invalid" | "delayed"
    retry_after_s: int = 0
    session_id: str | None = None
    session: sessions.WebSession | None = None
    account: accounts.Account | None = None


async def login(
    db: Database,
    settings: Settings,
    *,
    email_raw: object,
    password: str,
    ip_prefix: str,
    user_agent: str | None,
    old_session_id: str | None,
) -> LoginResult:
    email_norm = accounts.normalize_email(email_raw)[:accounts.MAX_EMAIL_LENGTH] if isinstance(email_raw, str) else ""
    email_key = ratelimit.email_key(email_norm)
    now = clock.now()

    def gate(conn: sqlite3.Connection):
        burst = ratelimit.hit_all(
            conn,
            [(ratelimit.LOGIN_PER_IP, ratelimit.ip_key(ip_prefix)), (ratelimit.LOGIN_BURST_PER_EMAIL, email_key)],
            now,
        )
        if not burst.allowed:
            return burst, None
        decision = ratelimit.login_gate(conn, email_key, now)
        if not decision.allowed:
            return decision, None
        return decision, (accounts.get_by_email(conn, email_norm) if email_norm else None)

    decision, account = await db.write_async(gate)
    if not decision.allowed:
        log.info("login delayed ip_prefix=%s retry_after_s=%d", ip_prefix, decision.retry_after_s)
        return LoginResult("delayed", decision.retry_after_s)

    usable = (
        account is not None and account.can_sign_in and account.has_password and len(password) <= passwords.MAX_LENGTH
    )
    checked = await passwords.verify_password_async(settings, account.password_hash if usable else None, password)
    if not checked.ok or account is None:
        await db.write_async(lambda conn: ratelimit.record_login_failure(conn, email_key, clock.now()))
        log.info("login failed ip_prefix=%s", ip_prefix)
        return LoginResult("invalid")

    new_hash = await passwords.hash_password_async(settings, password) if checked.needs_rehash else None
    signed_in = account

    def create(conn: sqlite3.Connection):
        ratelimit.record_login_success(conn, email_key)
        current = accounts.get_by_id(conn, signed_in.id)
        if current is None or not current.can_sign_in or current.password_hash != signed_in.password_hash:
            return None
        if new_hash:
            accounts.set_password_hash(conn, current.id, new_hash, current.password_changed_at)
        if old_session_id:
            previous = sessions.get(conn, old_session_id)
            if previous is not None:
                sessions.revoke(conn, previous.id_hash, now)
        session_id, session = sessions.create(
            conn, account_id=current.id, auth_epoch=current.auth_epoch, now=now, ip_prefix=ip_prefix, user_agent=user_agent
        )
        return session_id, session, current

    created = await db.write_async(create)
    if created is None:
        return LoginResult("invalid")
    session_id, session, current = created
    log.info("login account=%s rehashed=%s", current.id, bool(new_hash))
    return LoginResult("ok", session_id=session_id, session=session, account=current)


async def logout(db: Database, session_id_hash: str) -> None:
    await db.write_async(lambda conn: sessions.revoke(conn, session_id_hash, clock.now()))


# --------------------------------------------------------------------------- email tokens


def token_is_valid(db: Database, token: object, purposes: tuple[str, ...]) -> bool:
    with db.read() as conn:
        return email_tokens.find_valid(conn, token, purposes, clock.now()) is not None


@dataclass
class ConfirmResult:
    ok: bool
    purpose: str | None = None
    message: str = ""
    # The link was opened in the browser that signed up: that browser is signed in.
    session_id: str | None = None
    session: sessions.WebSession | None = None
    # Opened in another browser for a second sign-up that waits to take the account over: only the
    # code, typed where that sign-up happened, finishes it (that page cannot carry on by itself).
    code_needed: bool = False


async def confirm_email(
    db: Database,
    settings: Settings,
    token: object,
    *,
    browser: str | None = None,
    ip_prefix: str | None = None,
    user_agent: str | None = None,
    old_session_id: str | None = None,
) -> ConfirmResult:
    """Consume a `verify` or `email_change` token (POST only).

    A sign-up link opened in the browser that signed up (`browser` from its notice cookie) signs that
    browser in, exactly as the code does. Opened anywhere else, it only confirms the address and
    leaves the code usable, so the sign-up tab can still finish: by itself (`continue_signup`), or,
    for a second sign-up waiting to take the account over, with the code (`code_needed`).
    """
    now = clock.now()

    def transaction(conn: sqlite3.Connection) -> ConfirmResult:
        found = email_tokens.consume(conn, token, ("verify", "email_change"), now)
        if found is None:
            return ConfirmResult(False)
        account = accounts.get_by_id(conn, found.account_id)
        if account is None or not account.can_sign_in:
            return ConfirmResult(False)
        if found.purpose == "verify":
            row = email_tokens.code_token_for_link(conn, token)
            same_browser = (
                browser is not None and row is not None and row.browser_hash is not None
                and row.code_used_at is None and equals(row.browser_hash, sha256_hex(browser))
            )
            if same_browser and row is not None and email_tokens.use_code(conn, row.id, now):
                account = _finish_signup(conn, account, row, now)
                session_id, session = _start_session(
                    conn, account, now=now, ip_prefix=ip_prefix or "", user_agent=user_agent, old_session_id=old_session_id
                )
                return ConfirmResult(True, "verify", session_id=session_id, session=session)
            accounts.mark_verified(conn, account.id, now)
            waiting = row is not None and bool(row.new_password_hash) and row.code_used_at is None
            return ConfirmResult(True, "verify", code_needed=waiting)
        new_email = found.new_email or ""
        holder = accounts.get_by_email(conn, accounts.normalize_email(new_email))
        if holder is not None and holder.id != account.id:
            return ConfirmResult(False, "email_change", "That address now belongs to another account.")
        accounts.set_email(conn, account.id, new_email, now)
        email_tokens.invalidate(conn, account.id, "email_change", now)
        email_tokens.invalidate(conn, account.id, "verify", now)
        email_tokens.invalidate(conn, account.id, "reset", now, plain_resets_only=True)
        return ConfirmResult(True, "email_change")

    result = await db.write_async(transaction)
    log.info("email token confirmed ok=%s purpose=%s", result.ok, result.purpose)
    return result


async def resend_verification(db: Database, settings: Settings, account_id: str) -> str:
    """Returns "sent", "already_verified" or "limited"."""
    now = clock.now()
    key = ratelimit.account_key(account_id)

    def transaction(conn: sqlite3.Connection):
        account = accounts.get_by_id(conn, account_id)
        if account is None or account.email_verified:
            return "already_verified", []
        # The spacing rule is checked on its own first: an impatient second press must not eat
        # one of the day's five links, or a customer who cannot find the mail runs out of sends
        # without ever receiving five.
        if not ratelimit.hit(conn, ratelimit.VERIFY_RESEND_SPACING, key, now).allowed:
            return "limited", []
        if not ratelimit.hit(conn, ratelimit.VERIFY_RESEND_PER_ACCOUNT, key, now).allowed:
            return "limited", []
        # Signed in already, so the code needs no browser tie: it confirms the signed-in account.
        code = new_code()
        token = email_tokens.issue(
            conn, account.id, "verify", email_tokens.VERIFY_TTL_S, now, code_hash=code_digest(settings.secret_key, code)
        )
        content = mail.verify_email(settings, verify_link(settings, token), code)
        return "sent", [sender.queue_content(conn, settings, account.email, content, now)]

    outcome, queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    return outcome


# --------------------------------------------------------------------------- password reset


@dataclass
class ForgotResult:
    errors: dict[str, str] = field(default_factory=dict)
    retry_after_s: int | None = None


async def request_password_reset(db: Database, settings: Settings, *, email_raw: object, ip_prefix: str) -> ForgotResult:
    try:
        email_norm = accounts.normalize_email(accounts.validate_email(email_raw))
    except accounts.EmailError as exc:
        return ForgotResult({"email": str(exc)})
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        by_ip = ratelimit.hit(conn, ratelimit.FORGOT_PER_IP, ratelimit.ip_key(ip_prefix), now)
        if not by_ip.allowed:
            return by_ip, []
        by_email = ratelimit.hit(conn, ratelimit.FORGOT_PER_EMAIL, ratelimit.email_key(email_norm), now)
        account = accounts.get_by_email(conn, email_norm)
        if not by_email.allowed or account is None or not account.can_sign_in:
            return by_ip, []
        # Only the newest link may work: an older one sitting in the inbox (forwarded, or read on
        # a shared machine) stops working as soon as the customer asks for another. Revert links
        # from an email change are not touched; they are the customer's way back from a hijack.
        email_tokens.invalidate(conn, account.id, "reset", now, plain_resets_only=True)
        token = email_tokens.issue(conn, account.id, "reset", email_tokens.RESET_TTL_S, now)
        if not account.has_password:
            # Made with Google: there is nothing to reset, and "reset your password" would only puzzle
            # them. The page says the same as always; the email says how they sign in.
            content = mail.password_reset_google(settings, reset_link(settings, token))
        else:
            content = mail.password_reset(settings, reset_link(settings, token))
        return by_ip, [sender.queue_content(conn, settings, account.email, content, now)]

    decision, queued = await db.write_async(transaction)
    if not decision.allowed:
        return ForgotResult({"form": "Too many requests from your network. Try again later."}, decision.retry_after_s)
    await sender.after_commit_async(settings, queued)
    return ForgotResult()


@dataclass
class ResetResult:
    ok: bool
    errors: dict[str, str] = field(default_factory=dict)
    token_valid: bool = True
    restored_email: bool = False
    # A revert link was used but the old address now belongs to another account, so the account is
    # still on the address the link was meant to undo. Never reported as plain success.
    revert_blocked: bool = False
    # The browser that used the link, signed in with the new password (every other one is signed out).
    session_id: str | None = None
    session: sessions.WebSession | None = None


async def reset_password(
    db: Database,
    settings: Settings,
    *,
    token: object,
    password: str,
    ip_prefix: str | None = None,
    user_agent: str | None = None,
) -> ResetResult:
    """Set a new password from a reset (or revert) link; signs out every other session and device.

    The browser that used the link is signed in: it just proved it reads the account's inbox and
    chose the password, so asking for both again on the next page would only be a chore.
    """
    with db.read() as conn:
        found = email_tokens.find_valid(conn, token, ("reset",), clock.now())
        account = accounts.get_by_id(conn, found.account_id) if found is not None else None
    if found is None or account is None or not account.can_sign_in:
        return ResetResult(False, token_valid=False)
    policy = passwords.policy_errors(password, found.new_email or account.email)
    if policy:
        return ResetResult(False, {"password": policy[0]})
    password_hash = await passwords.hash_password_async(settings, password)
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        consumed = email_tokens.consume(conn, token, ("reset",), now)
        current = accounts.get_by_id(conn, consumed.account_id) if consumed is not None else None
        if consumed is None or current is None or not current.can_sign_in:
            return None
        accounts.set_password_hash(conn, current.id, password_hash, now)
        accounts.bump_auth_epoch(conn, current.id)
        sessions.revoke_all(conn, current.id, now)
        device_tokens.revoke_all(conn, current.id, now, "password_reset")
        accounts.mark_verified(conn, current.id, now)
        restored, blocked = False, False
        if consumed.is_revert:
            target = accounts.normalize_email(consumed.new_email or "")
            if target and target != current.email_norm:
                holder = accounts.get_by_email(conn, target)
                if holder is not None and not holder.email_verified:
                    # Whoever holds the address never proved they receive its mail; the person using
                    # this link just did (it was sent there). The unconfirmed account gives it up.
                    accounts.release_unverified_email(conn, holder.id, now)
                    sessions.revoke_all(conn, holder.id, now)
                    holder = None
                if holder is None:
                    accounts.set_email(conn, current.id, consumed.new_email or "", now)
                    restored = True
                else:
                    blocked = True
            email_tokens.invalidate(conn, current.id, "email_change", now)
            email_tokens.invalidate(conn, current.id, "reset", now)
        else:
            email_tokens.invalidate(conn, current.id, "reset", now, plain_resets_only=True)
        updated = accounts.get_by_id(conn, current.id)
        assert updated is not None
        session_id, session = sessions.create(
            conn, account_id=updated.id, auth_epoch=updated.auth_epoch, now=now, ip_prefix=ip_prefix, user_agent=user_agent
        )
        notice = sender.queue_content(conn, settings, updated.email, mail.password_reset_notice(settings), now)
        return restored, blocked, session_id, session, [notice]

    outcome = await db.write_async(transaction)
    if outcome is None:
        return ResetResult(False, token_valid=False)
    restored, blocked, session_id, session, queued = outcome
    await sender.after_commit_async(settings, queued)
    log.info("password reset account=%s revert=%s restored_email=%s blocked=%s", account.id, found.is_revert, restored, blocked)
    if blocked:
        log.warning("email-change revert could not restore the address for account=%s: another confirmed account holds it", account.id)
    return ResetResult(True, restored_email=restored, revert_blocked=blocked, session_id=session_id, session=session)


# --------------------------------------------------------------------------- signed-in changes


def signed_in_recently(session: sessions.WebSession | None, now: int | None = None) -> bool:
    return session is not None and (clock.now() if now is None else now) - session.created_at <= RECENT_SIGN_IN_S


async def confirm_identity(
    db: Database, settings: Settings, account_id: str, session: sessions.WebSession | None, password: str
) -> tuple[accounts.Account | None, str | None]:
    """The check before a sensitive change: the password, or for a Google-only account a recent sign-in."""
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None:
        return None, "Your password is incorrect."
    if account.has_password:
        return await check_current_password(db, settings, account_id, password)
    if signed_in_recently(session):
        return account, None
    return None, REAUTH_NEEDED


async def check_current_password(db: Database, settings: Settings, account_id: str, password: str) -> tuple[accounts.Account | None, str | None]:
    decision = await ratelimit.check(db, [(ratelimit.PASSWORD_CHECK_PER_ACCOUNT, ratelimit.account_key(account_id))])
    if not decision.allowed:
        return None, f"Too many attempts. Try again in {_minutes(decision.retry_after_s)} minutes."
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None:
        return None, "Your password is incorrect."
    checked = await passwords.verify_password_async(settings, account.password_hash, password[: passwords.MAX_LENGTH + 1])
    if not checked.ok:
        log.info("password check failed account=%s", account_id)
        return None, "That is not your current password."
    return account, None


@dataclass
class PasswordChangeResult:
    errors: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    session: sessions.WebSession | None = None
    added: bool = False  # a Google-only account set its first password


async def change_password(
    db: Database,
    settings: Settings,
    *,
    account_id: str,
    session: sessions.WebSession,
    current_password: str,
    new_password: str,
    ip_prefix: str,
    user_agent: str | None,
) -> PasswordChangeResult:
    """Voluntary change: keeps only the current browser signed in (rotated id).

    A Google-only account has no current password: after a recent sign-in it ADDS one, which signs
    nobody out (nothing that could be someone else's has changed) and sends its own notice.
    """
    account, problem = await confirm_identity(db, settings, account_id, session, current_password)
    if account is None:
        key = "password" if problem == REAUTH_NEEDED else "current_password"
        return PasswordChangeResult({key: problem or "Your password is incorrect."})
    policy = passwords.policy_errors(new_password, account.email)
    if policy:
        return PasswordChangeResult({"new_password": policy[0]})
    new_hash = await passwords.hash_password_async(settings, new_password)
    now = clock.now()
    adding = not account.has_password

    def transaction(conn: sqlite3.Connection):
        current = accounts.get_by_id(conn, account.id)
        live = sessions.still_valid(conn, session.id_hash, now)
        if current is None or live is None or current.password_hash != account.password_hash:
            return None
        if adding:
            accounts.set_password_hash(conn, current.id, new_hash, now)
            session_id, rotated = sessions.rotate(conn, live, now=now, ip_prefix=ip_prefix, user_agent=user_agent)
            return session_id, rotated, [sender.queue_content(conn, settings, current.email, mail.password_added_notice(settings), now)]
        accounts.set_password_hash(conn, current.id, new_hash, now)
        epoch = accounts.bump_auth_epoch(conn, current.id)
        sessions.revoke_all(conn, current.id, now, except_id_hash=live.id_hash)
        device_tokens.revoke_all(conn, current.id, now, "password_changed")
        email_tokens.invalidate(conn, current.id, "reset", now, plain_resets_only=True)
        session_id, rotated = sessions.rotate(
            conn, live, now=now, ip_prefix=ip_prefix, user_agent=user_agent, auth_epoch=epoch
        )
        return session_id, rotated, [sender.queue_content(conn, settings, current.email, mail.password_changed_notice(settings), now)]

    outcome = await db.write_async(transaction)
    if outcome is None:
        return PasswordChangeResult({"password": "Your password was not changed. Sign in again and retry."})
    session_id, rotated, queued = outcome
    await sender.after_commit_async(settings, queued)
    log.info("password %s account=%s", "added" if adding else "changed", account.id)
    return PasswordChangeResult(session_id=session_id, session=rotated, added=adding)


async def request_email_change(
    db: Database, settings: Settings, *, account_id: str, password: str, new_email_raw: object,
    session: sessions.WebSession | None = None,
) -> dict[str, str]:
    """Send a confirmation link to the new address and a 7-day revert link to the current one."""
    try:
        new_email = accounts.validate_email(new_email_raw)
    except accounts.EmailError as exc:
        return {"new_email": str(exc)}
    new_norm = accounts.normalize_email(new_email)
    if accounts.is_disposable(settings, new_norm):
        return {"new_email": "Use a permanent email address. Temporary inbox services cannot be used."}
    account, problem = await confirm_identity(db, settings, account_id, session, password)
    if account is None:
        return {"email" if problem == REAUTH_NEEDED else "email_password": problem or "Your password is incorrect."}
    if new_norm == account.email_norm:
        return {"new_email": "That is already your email address."}
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        limit = ratelimit.hit(conn, ratelimit.EMAIL_CHANGE_PER_ACCOUNT, ratelimit.account_key(account.id), now)
        current = accounts.get_by_id(conn, account.id)
        if not limit.allowed or current is None:
            return False, []
        email_tokens.invalidate(conn, current.id, "email_change", now)
        queued = []
        holder = accounts.get_by_email(conn, new_norm)
        if holder is None:
            token = email_tokens.issue(conn, current.id, "email_change", email_tokens.EMAIL_CHANGE_TTL_S, now, new_email=new_email)
            queued.append(sender.queue_content(conn, settings, new_email, mail.email_change_confirm(settings, verify_link(settings, token)), now))
        elif holder.can_sign_in:
            queued.append(sender.queue_content(conn, settings, holder.email, mail.email_in_use_notice(settings), now))
        revert = email_tokens.issue(conn, current.id, "reset", email_tokens.REVERT_TTL_S, now, new_email=current.email)
        queued.append(
            sender.queue_content(
                conn, settings, current.email,
                mail.email_change_notice(settings, accounts.mask_email(new_email), reset_link(settings, revert)), now,
            )
        )
        return True, queued

    allowed, queued = await db.write_async(transaction)
    if not allowed:
        return {"email": "Too many email changes today. Try again tomorrow."}
    await sender.after_commit_async(settings, queued)
    log.info("email change requested account=%s", account.id)
    return {}


async def sign_out_other_sessions(db: Database, account_id: str, session_id_hash: str) -> int:
    return await db.write_async(lambda conn: sessions.revoke_all(conn, account_id, clock.now(), except_id_hash=session_id_hash))


# --------------------------------------------------------------------------- sign in with Google


@dataclass
class GoogleResult:
    # "signed_in" | "confirm_new" (no account yet: ask before creating one) | "password_to_link"
    # (an account exists and Google is not authoritative for its address) | "refused"
    status: str
    message: str = ""
    retry_after_s: int | None = None
    session_id: str | None = None
    session: sessions.WebSession | None = None
    account: accounts.Account | None = None
    created: bool = False
    linked: bool = False
    claimed: bool = False
    queued: list = field(default_factory=list)


GOOGLE_UNVERIFIED = (
    "Google has not confirmed the email address on that Google account yet. Confirm it with Google, "
    "or create your account with your email address instead."
)
GOOGLE_CLOSED = "This Starling account cannot be signed in to. If you think that is a mistake, contact support."
GOOGLE_HELD = (
    "This email address was just moved off a Starling account and cannot start a new one for a few days. "
    "If that was you, sign in with your new address."
)
GOOGLE_OTHER_ACCOUNT = (
    "This Starling account signs in with a different Google account. Use that one, or sign in with your password."
)


def _start_session(conn: sqlite3.Connection, account: accounts.Account, *, now: int, ip_prefix: str,
                   user_agent: str | None, old_session_id: str | None):
    if old_session_id:
        previous = sessions.get(conn, old_session_id)
        if previous is not None:
            sessions.revoke(conn, previous.id_hash, now)
    return sessions.create(
        conn, account_id=account.id, auth_epoch=account.auth_epoch, now=now, ip_prefix=ip_prefix, user_agent=user_agent
    )


async def google_sign_in(
    db: Database,
    settings: Settings,
    *,
    user: GoogleUser,
    ip_prefix: str,
    user_agent: str | None,
    old_session_id: str | None,
    create_now: bool,
) -> GoogleResult:
    """Sign in the person Google vouches for, joining or creating the Starling account as the rules allow.

    1. A Google account already joined to a Starling account signs straight in.
    2. An address with an account: joined to Google at once when Google is authoritative for it
       (`GoogleUser.authoritative`). An account that never confirmed its address is handed to the
       Google user and its old password removed (see `accounts.claim_for_google`). For any other
       address, the account's password is asked for once ("password_to_link").
    3. No account: created now when the person came from the sign-up page (`create_now`), after one
       confirmation otherwise, because someone who meant to SIGN IN may have picked the wrong Google
       account, and a second, empty account is the last thing they want.
    """
    if not user.email_verified:
        return GoogleResult("refused", GOOGLE_UNVERIFIED)
    now = clock.now()
    email_norm = accounts.normalize_email(user.email)

    def transaction(conn: sqlite3.Connection) -> GoogleResult:
        identity = identities.find(conn, identities.GOOGLE, user.subject)
        if identity is not None:
            account = accounts.get_by_id(conn, identity.account_id)
            if account is None or not account.can_sign_in:
                return GoogleResult("refused", GOOGLE_CLOSED)
            identities.touch(conn, provider=identities.GOOGLE, subject=user.subject, email=user.email, now=now)
            session_id, session = _start_session(
                conn, account, now=now, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
            )
            return GoogleResult("signed_in", session_id=session_id, session=session, account=account)
        existing = accounts.get_by_email(conn, email_norm) if email_norm else None
        if existing is not None:
            if not existing.can_sign_in:
                return GoogleResult("refused", GOOGLE_CLOSED)
            if identities.for_account(conn, existing.id) is not None:
                return GoogleResult("refused", GOOGLE_OTHER_ACCOUNT)
            if not user.authoritative:
                return GoogleResult("password_to_link")
            claimed = not existing.email_verified
            if claimed:
                accounts.claim_for_google(conn, existing.id, now)
                sessions.revoke_all(conn, existing.id, now)
                device_tokens.revoke_all(conn, existing.id, now, "claimed_by_google")
                for purpose in ("verify", "reset", "email_change"):
                    email_tokens.invalidate(conn, existing.id, purpose, now)
            identities.link(
                conn, provider=identities.GOOGLE, subject=user.subject, account_id=existing.id, email=user.email, now=now
            )
            account = accounts.get_by_id(conn, existing.id)
            assert account is not None
            session_id, session = _start_session(
                conn, account, now=now, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
            )
            notice = sender.queue_content(conn, settings, account.email, mail.google_linked_notice(settings, user.email), now)
            return GoogleResult(
                "signed_in", session_id=session_id, session=session, account=account, linked=True, claimed=claimed,
                queued=[notice],
            )
        if email_tokens.address_is_held_for_a_revert(conn, email_norm, now):
            return GoogleResult("refused", GOOGLE_HELD)
        return GoogleResult("confirm_new")

    result = await db.write_async(transaction)
    await sender.after_commit_async(settings, result.queued)
    if result.status == "confirm_new" and create_now:
        return await create_google_account(
            db, settings, user=user, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
        )
    if result.status == "signed_in" and result.account is not None:
        log.info("google sign-in account=%s linked=%s claimed=%s", result.account.id, result.linked, result.claimed)
    return result


async def create_google_account(
    db: Database,
    settings: Settings,
    *,
    user: GoogleUser,
    ip_prefix: str,
    user_agent: str | None,
    old_session_id: str | None,
) -> GoogleResult:
    """A new account that signs in with Google: confirmed at once, no password, the limits of sign-up."""
    if not user.email_verified:
        return GoogleResult("refused", GOOGLE_UNVERIFIED)
    try:
        email = accounts.validate_email(user.email)
    except accounts.EmailError:
        return GoogleResult(
            "refused",
            "Starling cannot use the email address on that Google account. Create your account with your email address instead.",
        )
    email_norm = accounts.normalize_email(email)
    if accounts.is_disposable(settings, email_norm):
        return GoogleResult("refused", "Use a permanent email address. Temporary inbox services cannot be used.")
    limit = await ratelimit.check(db, [(ratelimit.SIGNUP_PER_IP, ratelimit.ip_key(ip_prefix))])
    if not limit.allowed:
        return GoogleResult("refused", "Too many sign-ups from your network. Try again later.", limit.retry_after_s)
    if settings.signup_global_per_hour > 0:
        overall = await ratelimit.check(
            db,
            [(ratelimit.Rule(ratelimit.SIGNUP_GLOBAL_BUCKET, settings.signup_global_per_hour, 3600), ratelimit.SIGNUP_GLOBAL_KEY)],
        )
        if not overall.allowed:
            log.warning("google sign-up refused: the site-wide ceiling of %d an hour is reached", settings.signup_global_per_hour)
            return GoogleResult("refused", "Sign-ups are busy right now. Please try again in a little while.", overall.retry_after_s)
    now = clock.now()

    def transaction(conn: sqlite3.Connection) -> GoogleResult:
        # Everything is checked again inside the writer: the confirmation page may have sat open
        # while another tab finished the same sign-in.
        if identities.find(conn, identities.GOOGLE, user.subject) is not None or accounts.get_by_email(conn, email_norm):
            return GoogleResult("retry")
        if email_tokens.address_is_held_for_a_revert(conn, email_norm, now):
            return GoogleResult("refused", GOOGLE_HELD)
        account = accounts.insert_account(conn, email=email, password_hash=accounts.NO_PASSWORD, ip_prefix=ip_prefix, now=now)
        accounts.mark_verified(conn, account.id, now)
        identities.link(conn, provider=identities.GOOGLE, subject=user.subject, account_id=account.id, email=email, now=now)
        created = accounts.get_by_id(conn, account.id)
        assert created is not None
        session_id, session = _start_session(
            conn, created, now=now, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id
        )
        log.info("google signup account=%s tenant_seq=%d", created.id, created.tenant_seq)
        return GoogleResult("signed_in", session_id=session_id, session=session, account=created, created=True)

    result = await db.write_async(transaction)
    if result.status == "retry":
        # Someone (most likely this person, in another tab) got there first: sign in to what exists.
        return await google_sign_in(
            db, settings, user=user, ip_prefix=ip_prefix, user_agent=user_agent, old_session_id=old_session_id,
            create_now=False,
        )
    return result


async def link_google(
    db: Database, settings: Settings, *, account_id: str, session: sessions.WebSession, user: GoogleUser
) -> str | None:
    """Join a Google account to the signed-in account (Security page). Returns an error message or None."""
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        live = sessions.still_valid(conn, session.id_hash, now)
        account = accounts.get_by_id(conn, account_id)
        if live is None or account is None or live.account_id != account_id:
            return "Sign in again, then connect Google.", []
        identity = identities.find(conn, identities.GOOGLE, user.subject)
        if identity is not None:
            if identity.account_id == account_id:
                identities.touch(conn, provider=identities.GOOGLE, subject=user.subject, email=user.email, now=now)
                return None, []
            return "That Google account already signs in to a different Starling account.", []
        if identities.for_account(conn, account_id) is not None:
            return "A Google account is already connected. Disconnect it first to connect another.", []
        identities.link(conn, provider=identities.GOOGLE, subject=user.subject, account_id=account_id, email=user.email, now=now)
        return None, [sender.queue_content(conn, settings, account.email, mail.google_linked_notice(settings, user.email), now)]

    error, queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    if error is None:
        log.info("google linked account=%s", account_id)
    return error


async def link_google_after_password(db: Database, settings: Settings, *, account: accounts.Account, user: GoogleUser) -> bool:
    """Finish a join that needed the password: the person just signed in with it (`auth.routes.login_submit`)."""
    if accounts.normalize_email(user.email) != account.email_norm:
        return False
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        if identities.find(conn, identities.GOOGLE, user.subject) is not None or identities.for_account(conn, account.id) is not None:
            return False, []
        identities.link(conn, provider=identities.GOOGLE, subject=user.subject, account_id=account.id, email=user.email, now=now)
        return True, [sender.queue_content(conn, settings, account.email, mail.google_linked_notice(settings, user.email), now)]

    linked, queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    if linked:
        log.info("google linked after password sign-in account=%s", account.id)
    return linked


async def reauthenticate_with_google(
    db: Database,
    settings: Settings,
    *,
    account_id: str,
    session: sessions.WebSession,
    user: GoogleUser,
    ip_prefix: str,
    user_agent: str | None,
):
    """A fresh Google sign-in for the account already signed in here: (session id, session) or an error message."""
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        identity = identities.for_account(conn, account_id)
        live = sessions.still_valid(conn, session.id_hash, now)
        if live is None or live.account_id != account_id:
            return "Sign in again to continue."
        if identity is None or identity.subject != user.subject:
            shown = f" ({accounts.mask_email(identity.email)})" if identity is not None else ""
            return f"That is a different Google account. Choose the one connected to Starling{shown}."
        identities.touch(conn, provider=identities.GOOGLE, subject=user.subject, email=user.email, now=now)
        return sessions.rotate(conn, live, now=now, ip_prefix=ip_prefix, user_agent=user_agent)

    return await db.write_async(transaction)


async def unlink_google(db: Database, settings: Settings, *, account_id: str, password: str) -> dict[str, str]:
    """Disconnect Google. Only an account with a password may, or it could never sign in again."""
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
        identity = identities.for_account(conn, account_id)
    if account is None or identity is None:
        return {}
    if not account.has_password:
        return {"google": "Set a password first, so you can still sign in once Google is disconnected."}
    confirmed, problem = await check_current_password(db, settings, account_id, password)
    if confirmed is None:
        return {"google_password": problem or "Your password is incorrect."}
    now = clock.now()

    def transaction(conn: sqlite3.Connection):
        if not identities.unlink(conn, account_id):
            return []
        return [sender.queue_content(conn, settings, confirmed.email, mail.google_unlinked_notice(settings), now)]

    queued = await db.write_async(transaction)
    await sender.after_commit_async(settings, queued)
    log.info("google unlinked account=%s", account_id)
    return {}


def google_identity(db: Database, account_id: str) -> identities.Identity | None:
    with db.read() as conn:
        return identities.for_account(conn, account_id)
