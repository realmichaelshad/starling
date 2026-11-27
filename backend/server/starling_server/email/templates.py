"""Plain-text transactional emails.

Bodies contain links with single-use tokens, so they are never logged. Every function returns
an `EmailContent` whose `template` name is stored in `email_outbox.template`.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..auth.accounts import mask_email
from ..config import Settings

SITE_NAME = "Starling"


@dataclass(frozen=True)
class EmailContent:
    template: str
    subject: str
    body: str


def _footer(settings: Settings) -> str:
    return f"\n\n-- \n{SITE_NAME}\n{settings.public_url}\nQuestions: {settings.support_email}\n"


def link(settings: Settings, path: str) -> str:
    return settings.public_url + path


def spaced_code(code: str) -> str:
    """"482915" as people read it: "482 915"."""
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def verify_email(settings: Settings, verify_link: str, code: str | None = None) -> EmailContent:
    """Sent at sign-up and on resend. With a code, the person can finish in the tab where they signed up."""
    if code:
        body = (
            "Welcome to Starling.\n\n"
            f"Your confirmation code is {spaced_code(code)}\n\n"
            "Enter it on the Starling page where you signed up, or confirm with this link:\n\n"
            f"{verify_link}\n\n"
            "The code and the link work for 24 hours. If you did not create a Starling account, you can ignore this email."
        )
        return EmailContent("verify_email", f"Your Starling code: {spaced_code(code)}", body + _footer(settings))
    body = (
        "Welcome to Starling.\n\n"
        "Confirm your email address to start using Starling:\n\n"
        f"{verify_link}\n\n"
        "The link works for 24 hours. If you did not create a Starling account, you can ignore this email."
    )
    return EmailContent("verify_email", "Confirm your email for Starling", body + _footer(settings))


def signup_existing(settings: Settings, *, with_google: bool = False) -> EmailContent:
    if with_google:
        body = (
            "Someone tried to create a Starling account with this email address.\n\n"
            "If that was you: you already have an account, and you sign in with Google. On the sign-in page, "
            "choose Continue with Google:\n\n"
            f"{link(settings, '/login')}\n\n"
            "If it was not you, no action is needed. Your account has not changed."
        )
        return EmailContent("signup_existing", "You already have a Starling account", body + _footer(settings))
    body = (
        "Someone tried to create a Starling account with this email address.\n\n"
        "If that was you: you already have an account. Sign in here:\n\n"
        f"{link(settings, '/login')}\n\n"
        "Forgot your password? Reset it here:\n\n"
        f"{link(settings, '/forgot-password')}\n\n"
        "If it was not you, no action is needed. Your account has not changed."
    )
    return EmailContent("signup_existing", "You already have a Starling account", body + _footer(settings))


def password_reset(settings: Settings, reset_link: str) -> EmailContent:
    body = (
        "You asked to reset your Starling password. Choose a new password here (the link works for 60 minutes):\n\n"
        f"{reset_link}\n\n"
        "If you did not ask for this, ignore this email; your password stays the same."
    )
    return EmailContent("password_reset", "Reset your Starling password", body + _footer(settings))


def password_reset_google(settings: Settings, reset_link: str) -> EmailContent:
    body = (
        "You asked to reset your Starling password, but you sign in to Starling with Google, so there is "
        "no password to reset. On the sign-in page, choose Continue with Google:\n\n"
        f"{link(settings, '/login')}\n\n"
        "If you would like a password as well, choose one here (the link works for 60 minutes):\n\n"
        f"{reset_link}\n\n"
        "If you did not ask for this, ignore this email; nothing has changed."
    )
    return EmailContent("password_reset_google", "Sign in to Starling with Google", body + _footer(settings))


def password_reset_notice(settings: Settings) -> EmailContent:
    body = (
        "Your Starling password was reset, and every browser and connected extension was signed out.\n\n"
        "If you did not do this, reset your password again right away and contact us:\n\n"
        f"{link(settings, '/forgot-password')}"
    )
    return EmailContent("password_reset_notice", "Your Starling password was reset", body + _footer(settings))


def password_changed_notice(settings: Settings) -> EmailContent:
    body = (
        "Your Starling password was changed. Other browsers and connected extensions were signed out.\n\n"
        "If you did not do this, reset your password right away:\n\n"
        f"{link(settings, '/forgot-password')}"
    )
    return EmailContent("password_changed_notice", "Your Starling password was changed", body + _footer(settings))


def password_added_notice(settings: Settings) -> EmailContent:
    body = (
        "A password was added to your Starling account. You can now sign in with your email address "
        "and password, as well as with Google.\n\n"
        "If you did not do this, reset your password right away:\n\n"
        f"{link(settings, '/forgot-password')}"
    )
    return EmailContent("password_added_notice", "A password was added to your Starling account", body + _footer(settings))


def google_linked_notice(settings: Settings, google_email: str) -> EmailContent:
    # The Google address may differ from the account's. It is masked, like every address in a notice.
    body = (
        f"Sign in with Google is now turned on for your Starling account (Google account {mask_email(google_email)}).\n\n"
        "If you did not do this, sign in, open Security and disconnect Google, then reset your password:\n\n"
        f"{link(settings, '/account/security')}"
    )
    return EmailContent("google_linked_notice", "Google sign-in was added to your Starling account", body + _footer(settings))


def google_unlinked_notice(settings: Settings) -> EmailContent:
    body = (
        "Google was disconnected from your Starling account. Sign in with your email address and password from now on.\n\n"
        "If you did not do this, reset your password right away:\n\n"
        f"{link(settings, '/forgot-password')}"
    )
    return EmailContent("google_unlinked_notice", "Google was disconnected from your Starling account", body + _footer(settings))


def account_deleted(settings: Settings, when: str, plan_name: str | None) -> EmailContent:
    cancelled = f"Your {plan_name} subscription was cancelled, and you will not be charged again.\n\n" if plan_name else ""
    body = (
        f"Your Starling account and the data Starling kept for drafting were deleted on {when}.\n\n"
        f"{cancelled}"
        "Billing records that must be kept for tax reasons are kept, as the Privacy Policy explains:\n\n"
        f"{link(settings, '/privacy')}\n\n"
        "If you did not do this, reply to this email straight away."
    )
    return EmailContent("account_deleted", "Your Starling account is deleted", body + _footer(settings))


def email_change_confirm(settings: Settings, confirm_link: str) -> EmailContent:
    body = (
        "Confirm this address as the new sign-in email for your Starling account:\n\n"
        f"{confirm_link}\n\n"
        "The link works for 24 hours. If you did not ask for this, ignore this email."
    )
    return EmailContent("email_change_confirm", "Confirm your new Starling email", body + _footer(settings))


def email_change_notice(settings: Settings, new_email_masked: str, revert_link: str) -> EmailContent:
    body = (
        f"Someone asked to change the sign-in email of your Starling account to {new_email_masked}.\n\n"
        "If that was you, no action is needed.\n\n"
        "If it was not you, secure your account within 7 days with this link. It keeps this address, "
        "signs out every browser and extension, and lets you choose a new password:\n\n"
        f"{revert_link}"
    )
    return EmailContent("email_change_notice", "Your Starling sign-in email is changing", body + _footer(settings))


def email_in_use_notice(settings: Settings) -> EmailContent:
    body = (
        "Someone tried to move another Starling account to this email address. Nothing was changed.\n\n"
        "If that was you, sign in with this address instead:\n\n"
        f"{link(settings, '/login')}"
    )
    return EmailContent("email_in_use_notice", "Someone tried to use your email on Starling", body + _footer(settings))
