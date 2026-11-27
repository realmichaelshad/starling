"""Realistic render contexts for every website template.

Shapes follow docs/architecture/CONTRACTS.md section 10 and what the gateway sends today
(backend/server/starling_server/web/render.py, auth/routes.py, devices/routes.py). Keys the gateway
does not send yet are marked "proposed" below and in the comment at the top of each template.
Used by web/tests/test_templates.py and `python scripts/check_site.py --templates`. All people,
companies and ids are fictional; times are fixed so rendering is deterministic.

SAMPLES is a list of (template name, label, context).
"""
from __future__ import annotations

import copy

NOW = 1_789_430_400  # 2026-09-15 00:00:00 UTC
DAY = 86_400
CSRF = "csrf-sample-4f1c2a"
PUBLIC_URL = "https://starling.example"
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
INSTALL_URL = f"https://chromewebstore.google.com/detail/starling/{EXTENSION_ID}"  # proposed site key
SUPPORT_EMAIL = "support@starling.example"

UNIT = {"name": "draft", "label": "drafts"}


def _plan(code: str, name: str, credits: int, cents: int, per_minute: int, per_day: int) -> dict:
    """Same keys as web/render.py plan_dict. The free plan has no currency or interval."""
    paid = cents > 0
    return {"code": code, "name": name, "monthly_credits": credits, "amount_cents": cents,
            "currency": "usd" if paid else None, "interval": "month" if paid else None, "is_paid": paid,
            "price_label": f"${cents // 100}" if paid else "Free", "rate_per_minute": per_minute,
            "rate_per_day": per_day}


PLANS = [  # the gateway passes public plans only
    _plan("free", "Free", 25, 0, 3, 10),
    _plan("starter", "Starter", 250, 900, 6, 60),
    _plan("pro", "Pro", 800, 1900, 10, 150),
]
LEGACY_PLAN = _plan("studio_legacy", "Studio Legacy", 2000, 4900, 20, 400)  # not public: never in `plans`

# site.legal as the gateway sends it: LegalDetails fields with their development defaults.
LEGAL_GATEWAY_DEFAULTS = {
    "company_name": "[[OPERATOR_COMPANY_NAME]]", "registered_address": "[[OPERATOR_REGISTERED_ADDRESS]]",
    "contact_email": "[[OPERATOR_CONTACT_EMAIL]]", "privacy_contact": "[[OPERATOR_PRIVACY_CONTACT]]",
    "jurisdiction": "[[OPERATOR_JURISDICTION]]", "governing_law_venue": "[[OPERATOR_GOVERNING_LAW_VENUE]]",
    "effective_date": "[[OPERATOR_EFFECTIVE_DATE]]", "hosting_provider": "[[HOSTING_PROVIDER]]",
    "email_provider": "[[EMAIL_PROVIDER]]", "ai_subprocessors": "OpenAI",
}
# A completed production configuration, including the two proposed fields.
LEGAL_FILLED = {
    "company_name": "Example Apps LLC",
    "registered_address": "100 Example Avenue, Suite 5, Dover, DE 19901, United States",
    "contact_email": "legal@starling.example",
    "privacy_contact": "privacy@starling.example",
    "jurisdiction": "the State of Delaware, United States",
    "governing_law_venue": "the state and federal courts located in Delaware",
    "effective_date": "October 1, 2026",
    "hosting_provider": "Example Cloud Hosting Inc.",
    "email_provider": "Example Mail Delivery Inc.",
    "ai_subprocessors": "OpenAI",
    "retention_periods": "account records are deleted within 30 days of account deletion; billing records are kept for 7 years; service logs are kept for 30 days; backups are overwritten within 35 days.",  # proposed
    "refund_policy": "If you ask within 14 days of your first payment for a plan, we refund that payment in full. Other payments are not refunded, and you keep access until the end of the period you paid for.",  # proposed
}
LEGAL_GATEWAY_FILLED = {k: v for k, v in LEGAL_FILLED.items() if k in LEGAL_GATEWAY_DEFAULTS}

# web/render.py subprocessors_context: {name, purpose}.
SUBPROCESSORS_DEV = [
    {"name": "[[HOSTING_PROVIDER]]", "purpose": "Hosting and data storage"},
    {"name": "[[EMAIL_PROVIDER]]", "purpose": "Transactional email"},
    {"name": "Stripe", "purpose": "Payments and billing"},
    {"name": "OpenAI", "purpose": "AI text generation"},
]
SUBPROCESSORS_PRODUCTION = [
    {"name": "Example Cloud Hosting Inc.", "purpose": "Hosting and data storage"},
    {"name": "Example Mail Delivery Inc.", "purpose": "Transactional email"},
    {"name": "Stripe", "purpose": "Payments and billing"},
    {"name": "OpenAI", "purpose": "AI text generation"},
]
SUBPROCESSORS_DETAILED = [  # proposed optional keys: data, location
    {**SUBPROCESSORS_PRODUCTION[0], "data": "All account and drafting data", "location": "United States"},
    {**SUBPROCESSORS_PRODUCTION[1], "data": "Email address and message content", "location": "European Union"},
    {**SUBPROCESSORS_PRODUCTION[2], "data": "Email address and billing details", "location": "United States"},
    {**SUBPROCESSORS_PRODUCTION[3], "data": "Content sent for drafting", "location": "United States"},
]

MEMBER = {"email": "casey@example.com", "email_masked": "c***@example.com", "is_admin": False}
OWNER = {"email": "owner@starling.example", "email_masked": "o***@starling.example", "is_admin": True}
ELEVATED = {"elevated_until": NOW + 600}  # proposed optional admin context
ACCOUNT_ID = "3f6c1b8e-2a4d-4c6b-9e1f-7a2b5c8d9e01"

DOC_SLUGS = ("getting-started", "writing", "panel", "plans-and-usage", "privacy-and-data", "troubleshooting")
LEGAL_PAGES = ("privacy", "terms", "refunds", "subprocessors", "acceptable-use", "contact")


def plan(code: str) -> dict:
    return next(p for p in (*PLANS, LEGACY_PLAN) if p["code"] == code)


def make_site(env: str = "development", *, install_url: str | None = None, support_email: str = SUPPORT_EMAIL,
              legal: dict | None = None, google: bool = False) -> dict:
    site = {
        "name": "Starling",
        "public_url": PUBLIC_URL,
        "support_email": support_email,
        "env": env,
        "legal": dict(legal if legal is not None else (LEGAL_FILLED if env == "production" else LEGAL_GATEWAY_DEFAULTS)),
        "google_sign_in": google,  # web/render.py site_context: GOOGLE_CLIENT_ID and _SECRET are set
        "web_search": True,  # web/render.py site_context: WEB_SEARCH (on by default)
    }
    if install_url:
        site["extension_install_url"] = install_url  # proposed
    return site


def base(path: str, *, user: dict | None = None, env: str = "development", flash: list | None = None,
         install_url: str | None = None, support_email: str = SUPPORT_EMAIL, legal: dict | None = None,
         google: bool = False, **extra) -> dict:
    context = {
        "site": make_site(env, install_url=install_url, support_email=support_email, legal=legal, google=google),
        "user": copy.deepcopy(user),
        "csrf_token": CSRF,
        "flash": list(flash or []),
        "request_path": path,
    }
    context.update(copy.deepcopy(extra))
    return context


def make_state(code: str = "starter", *, used: int = 12, reserved: int = 0, allowance: int | None = None,
               remaining: int | None = None, verified: bool = True, plan_status: str | None = None,
               account_status: str = "active", scheduled: dict | None = None, cancel_at: int | None = None,
               payment_problem: bool = False, payment_issue: dict | None = None,
               fair_use_blocked: bool = False, service_status: str = "ok",
               service_message: str | None = None, service_scope: str | None = None,
               accepted_version: str | None = "2026-09-22.1", passive_learning: bool = False) -> dict:
    """Account state exactly as CONTRACTS 6.2 and usage/service.py account_state() build it."""
    p = plan(code)
    allowance = p["monthly_credits"] if allowance is None else allowance
    if remaining is None:
        remaining = max(0, allowance - used - reserved)
    period_start, period_end = NOW - 12 * DAY, NOW + 18 * DAY
    return {
        "account": {"id": ACCOUNT_ID, "email_masked": "c***@example.com", "email_verified": verified,
                    "status": account_status},
        "plan": {"code": code, "name": p["name"], "status": plan_status or ("free" if code == "free" else "active"),
                 "interval": p["interval"], "period_start": period_start, "period_end": period_end,
                 "cancel_at": cancel_at, "scheduled_change": scheduled, "payment_problem": payment_problem,
                 "payment_issue": payment_issue,
                 "upgradable": any(q["amount_cents"] > p["amount_cents"] for q in PLANS)},
        "usage": {"unit": "draft", "unit_label": "drafts", "allowance": allowance, "used": used,
                  "reserved": reserved, "remaining": remaining, "resets_at": period_end,
                  "near_limit": (used + reserved) * 5 >= allowance * 4 and remaining > 0,
                  "fair_use_blocked": fair_use_blocked, "per_request": 1},
        "consent": {"required_version": "2026-09-22.1", "accepted_version": accepted_version,
                    "passive_learning": passive_learning},
        "service": {"status": service_status,
                    "scope": (service_scope or "all") if service_status == "paused" else None,
                    "message": service_message,
                    "retry_at": NOW + 5 * 3600 if service_status == "paused" else None},
        "extension": {"min_version": "1.0.0"},
    }


# CONTRACTS 6.2 `plan.payment_issue`: which payment needs the customer, and for which plan.
FIRST_PAYMENT = {"kind": "first_payment", "plan_code": "starter", "plan_name": "Starter"}
RENEWAL = {"kind": "renewal", "plan_code": "starter", "plan_name": "Starter"}
LAPSED = {"kind": "lapsed", "plan_code": "starter", "plan_name": "Starter"}

SAMPLES: list[tuple[str, str, dict]] = []


def add(template: str, label: str, context: dict) -> None:
    if template == "account/overview.html":
        context.setdefault("devices_count", 1)  # account/routes.py overview: connected browsers
    if template == "account/billing.html":
        context.setdefault("returned_from", None)  # account/routes.py billing_page: back from Stripe, or None
        context.setdefault("chosen_plan", None)  # ...and a plan picked on the pricing page before sign-up
    SAMPLES.append((template, label, context))


# ---------------------------------------------------------------- marketing
add("index.html", "signed out, gateway context", base("/", plans=PLANS, unit=UNIT))
add("index.html", "signed in, production, unit and store link", base(
    "/", user=MEMBER, env="production", install_url=INSTALL_URL, plans=PLANS, unit=UNIT))
add("pricing.html", "signed out", base("/pricing", plans=PLANS, unit=UNIT, state=None))
_rehearsal = base("/pricing", env="production", plans=PLANS, unit=UNIT, state=None)
_rehearsal["site"]["payments_test_mode"] = True  # production on Stripe's sandbox (STRIPE_ALLOW_TEST_MODE)
add("pricing.html", "payments rehearsal", _rehearsal)
add("pricing.html", "free", base("/pricing", user=MEMBER, plans=PLANS, unit=UNIT, state=make_state("free", used=3)))
add("pricing.html", "starter near limit", base("/pricing", user=MEMBER, plans=PLANS, unit=UNIT,
                                               state=make_state("starter", used=205, reserved=1)))
add("pricing.html", "pro limit reached from extension", base(
    "/pricing", user=MEMBER, plans=PLANS, unit=UNIT, state=make_state("pro", used=800),
    flash=[{"level": "warning", "text": "You have used all your drafts for this period."}]))
add("pricing.html", "free fair use", base("/pricing", user=MEMBER, plans=PLANS, unit=UNIT,
                                          state=make_state("free", used=9, fair_use_blocked=True)))
add("pricing.html", "starter cancelling", base("/pricing", user=MEMBER, plans=PLANS, unit=UNIT,
                                              state=make_state("starter", used=40, cancel_at=NOW + 18 * DAY)))
add("pricing.html", "legacy plan subscriber", base("/pricing", user=MEMBER, plans=PLANS, unit=UNIT,
                                                   state=make_state("studio_legacy", used=40)))

# ---------------------------------------------------------------- docs
for slug in DOC_SLUGS:
    add(f"docs/{slug}.html", "signed out", base(f"/docs/{slug}"))
add("docs/getting-started.html", "signed in, store link", base("/docs/getting-started", user=MEMBER, install_url=INSTALL_URL))
add("docs/troubleshooting.html", "support address not configured", base(
    "/docs/troubleshooting", support_email="[[OPERATOR_CONTACT_EMAIL]]"))

# ---------------------------------------------------------------- legal
for name in LEGAL_PAGES:
    add(f"legal/{name}.html", "draft", base(f"/{name}", subprocessors=SUBPROCESSORS_DEV,
                                            support_email="[[OPERATOR_CONTACT_EMAIL]]"))
    add(f"legal/{name}.html", "production", base(f"/{name}", env="production", subprocessors=SUBPROCESSORS_PRODUCTION))
add("legal/subprocessors.html", "detailed columns", base("/subprocessors", env="production", subprocessors=SUBPROCESSORS_DETAILED))
add("legal/subprocessors.html", "empty list", base("/subprocessors", env="production", subprocessors=[]))

# ---------------------------------------------------------------- refusals (security/errors.py page_error_response)
add("errors/problem.html", "expired form", base("/account/security/password", user=MEMBER, status=403,
    title="This page expired",
    message="For your security, forms stop working after a while, or after you sign in or out in another tab. "
            "Reload the page and try again.",
    action={"label": "Reload the page", "href": "/account/security"}, secondary=None))
add("errors/problem.html", "unconfirmed address", base("/billing/checkout", user=MEMBER, status=403,
    title="Confirm your email first",
    message="Confirm your email address before you continue. We sent a 6-digit code and a link to casey@example.com.",
    action={"label": "Enter the code", "href": "/verify-email/sent"},
    secondary={"label": "Go to your account", "href": "/account"}))
add("errors/problem.html", "too many attempts", base("/connect", status=429, title="Too many attempts",
    message="Too many connection attempts. Try again in 4 minutes.", action={"label": "Go back", "href": "/"},
    secondary=None))
add("errors/problem.html", "unavailable", base("/account", user=MEMBER, status=503,
    title="Starling is briefly unavailable", message="The website is not available right now. Please try again later.",
    action={"label": "Try again", "href": "/account"}, secondary=None))

# ---------------------------------------------------------------- auth (context keys as auth/routes.py sends them)
add("auth/signup.html", "empty", base("/signup", errors={}, form={"email": ""}, next="/account"))
add("auth/signup.html", "errors", base("/signup", errors={
    "form": "Too many sign-ups from your network. Try again later.", "email": "Enter a valid email address.",
    "password": "Use at least 12 characters."}, form={"email": "casey@example"}, next="/account"))
add("auth/signup.html", "with Google", base("/signup", google=True, errors={}, form={"email": ""}, next="/account"))
add("auth/signup.html", "with Google, from connect", base(
    "/signup", google=True, errors={}, form={"email": ""}, next=f"/connect?client_id={EXTENSION_ID}"))
add("auth/login.html", "empty", base("/login", errors={}, form={"email": ""}, next="/account", google_link=None))
add("auth/login.html", "from connect with error", base(
    "/login", errors={"form": "The email or password is incorrect."}, form={"email": "casey@example.com"},
    next=f"/connect?client_id={EXTENSION_ID}", google_link=None))
add("auth/login.html", "field errors", base("/login", errors={"email": "Enter your email address.",
                                                             "password": "Enter your password."}, form={"email": ""},
                                            next="/account", google_link=None))
add("auth/login.html", "with Google", base("/login", google=True, errors={}, form={"email": ""}, next="/account",
                                           google_link=None))
add("auth/login.html", "with Google, wrong password", base(
    "/login", google=True, form={"email": "casey@example.com"}, next="/account", google_link=None,
    errors={"form": "The email or password is incorrect. If you signed up with Google, use Continue with Google."}))
add("auth/login.html", "Google waiting for the password", base(
    "/login", google=True, errors={}, form={"email": "casey@example.com"}, next="/account",
    google_link={"email": "casey@example.com", "email_masked": "c***@example.com"}))
add("auth/google_new.html", "confirm", base("/auth/google/new", google=True, email="casey.writes@gmail.com",
                                            next="/account", errors={}))
add("auth/google_new.html", "refused", base("/auth/google/new", google=True, email="casey.writes@gmail.com",
                                            next=f"/connect?client_id={EXTENSION_ID}",
                                            errors={"form": "Sign-ups are busy right now. Please try again in a little while."}))
add("auth/verify_sent.html", "signed in", base("/verify-email/sent", user=MEMBER, email_masked="c***@example.com",
                                               email="casey@example.com", code_form=True, confirmed=False,
                                               mail_link=None, errors={}))
add("auth/verify_sent.html", "signed out without notice", base("/verify-email/sent", email_masked=None, email=None,
                                                               code_form=False, confirmed=False, mail_link=None,
                                                               errors={}))
add("auth/verify_sent.html", "just signed up with Gmail", base(
    "/verify-email/sent", email_masked="c***@gmail.com", email="casey.writes@gmail.com", code_form=True, confirmed=False,
    mail_link={"label": "Open Gmail", "url": "https://mail.google.com/mail/u/0/#inbox"}, errors={}))
add("auth/verify_sent.html", "wrong code", base(
    "/verify-email/sent", email_masked="c***@example.com", email="casey@example.com", code_form=True, confirmed=False,
    mail_link=None, errors={"code": "That code is not right. Check the newest email from Starling."}))
add("auth/verify_sent.html", "confirmed on the phone, signed out", base(
    "/verify-email/sent", email_masked="c***@example.com", email="casey@example.com", code_form=True, confirmed=True,
    mail_link=None, errors={}))
add("auth/verify_sent.html", "signed in, already confirmed", base(
    "/verify-email/sent", user=MEMBER, email_masked="c***@example.com", email="casey@example.com", code_form=False,
    confirmed=True, mail_link=None, errors={}))
add("auth/verify.html", "valid", base("/verify-email", token_valid=True, token="vt_sample_7d2e", already_confirmed=False))
add("auth/verify.html", "invalid, signed in", base("/verify-email", user=MEMBER, token_valid=False, token="",
                                                   already_confirmed=False))
add("auth/verify.html", "invalid, signed out", base("/verify-email", token_valid=False, token="", already_confirmed=False))
add("auth/verify.html", "used again, already confirmed", base("/verify-email", user=MEMBER, token_valid=False, token="",
                                                              already_confirmed=True))
add("auth/forgot.html", "form", base("/forgot-password", sent=False, errors={}, form={"email": ""}, mail_link=None))
add("auth/forgot.html", "sent", base("/forgot-password", sent=True, errors={}, form={"email": "casey@example.com"},
                                     mail_link=None))
add("auth/forgot.html", "sent to Gmail", base("/forgot-password", sent=True, errors={}, form={"email": "casey.writes@gmail.com"},
                                              mail_link={"label": "Open Gmail", "url": "https://mail.google.com/mail/u/0/#inbox"}))
add("auth/forgot.html", "field error", base("/forgot-password", sent=False, errors={"email": "Enter a valid email address."},
                                            form={"email": "casey@"}, mail_link=None))
add("auth/forgot.html", "rate limited", base("/forgot-password", sent=False, errors={
    "form": "Too many requests from your network. Try again later."}, form={"email": "casey@example.com"}, mail_link=None))
add("auth/reset.html", "valid", base("/reset-password", token_valid=True, token="rt_sample_91ab", errors={}))
add("auth/reset.html", "weak password", base("/reset-password", token_valid=True, token="rt_sample_91ab",
                                             errors={"password": "Use at least 12 characters."}))
add("auth/reset.html", "invalid", base("/reset-password", token_valid=False, token="", errors={}))

# ---------------------------------------------------------------- account (proposed error keys per form)
add("account/overview.html", "free, unverified", base("/account", user=MEMBER, devices_count=0, state=make_state(
    "free", used=0, verified=False, accepted_version=None)))
add("account/overview.html", "new, confirmed, nothing connected", base("/account", user=MEMBER, devices_count=0,
                                                                      install_url=INSTALL_URL,
                                                                      state=make_state("free", used=0)))
add("account/overview.html", "new, connected, no draft yet", base("/account", user=MEMBER, devices_count=1,
                                                                 state=make_state("free", used=0)))
add("account/overview.html", "starter", base("/account", user=MEMBER, state=make_state("starter", used=12),
                                            flash=[{"level": "success", "text": "Your email address is confirmed."}]))
add("account/overview.html", "starter near limit with request in progress", base("/account", user=MEMBER, state=make_state(
    "starter", used=203, reserved=1, passive_learning=True)))
add("account/overview.html", "pro limit reached, store link", base("/account", user=MEMBER, install_url=INSTALL_URL,
                                                                  state=make_state("pro", used=800)))
add("account/overview.html", "starter past due", base("/account", user=MEMBER, state=make_state(
    "starter", used=40, plan_status="past_due", payment_problem=True, payment_issue=RENEWAL)))
add("account/overview.html", "free, first payment failed", base("/account", user=MEMBER, state=make_state(
    "free", used=4, payment_problem=True, payment_issue=FIRST_PAYMENT)))
add("account/overview.html", "free, renewal lapsed", base("/account", user=MEMBER, state=make_state(
    "free", used=4, payment_problem=True, payment_issue=LAPSED)))
add("account/overview.html", "pro scheduled downgrade", base("/account", user=MEMBER, state=make_state(
    "pro", used=90, scheduled={"plan_code": "starter", "effective_at": NOW + 18 * DAY})))
add("account/overview.html", "suspended while paused", base("/account", user=MEMBER, state=make_state(
    "free", used=5, account_status="suspended", plan_status="suspended", service_status="paused",
    service_message="Drafting is paused for maintenance until 14:00 UTC.")))
add("account/overview.html", "fair use and cancelling", base("/account", user=MEMBER, state=make_state(
    "starter", used=120, fair_use_blocked=True, cancel_at=NOW + 18 * DAY, accepted_version="2026-08-01.1")))
add("account/overview.html", "free plan paused for the day", base("/account", user=MEMBER, devices_count=1, state=make_state(
    "free", used=4, service_status="paused", service_scope="free",
    service_message="Drafting on the Free plan is paused for the rest of the day. Paid plans keep drafting.")))
add("account/overview.html", "grant beyond allowance", base("/account", user=MEMBER, state=make_state(
    "free", used=30, remaining=20)))

add("account/billing.html", "free", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=False,
                                         state=make_state("free", used=4), returned_from=None))
add("account/billing.html", "plan chosen before sign-up", base("/account/billing", user=MEMBER, plans=PLANS,
                                                               invoices_url_available=False, chosen_plan=plan("starter"),
                                                               state=make_state("free", used=0)))
add("account/billing.html", "back from cancelling", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                         returned_from="cancel",
                                                         state=make_state("starter", used=40, cancel_at=NOW + 18 * DAY)))
add("account/billing.html", "back from a new card", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                         returned_from="card", state=make_state("starter", used=40)))
add("account/billing.html", "back from upgrading", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                        returned_from="upgrade", state=make_state("pro", used=90)))
add("account/billing.html", "pro scheduled downgrade", base(
    "/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
    state=make_state("pro", used=90, scheduled={"plan_code": "starter", "effective_at": NOW + 18 * DAY})))
add("account/billing.html", "starter past due", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                     state=make_state("starter", used=40, plan_status="past_due", payment_problem=True,
                                                                      payment_issue=RENEWAL)))
add("account/billing.html", "free, first payment failed", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                                state=make_state("free", used=4, payment_problem=True, payment_issue=FIRST_PAYMENT)))
add("account/billing.html", "free, first payment failed, unknown plan", base(
    "/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
    state=make_state("free", used=4, payment_problem=True, payment_issue={"kind": "first_payment", "plan_code": None, "plan_name": None})))
add("account/billing.html", "free, first payment failed, older gateway", base(
    "/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True, state=make_state("free", used=4, payment_problem=True)))
add("account/billing.html", "free, renewal lapsed", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                          state=make_state("free", used=4, payment_problem=True, payment_issue=LAPSED)))
add("account/billing.html", "starter cancelling", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                       state=make_state("starter", used=10, cancel_at=NOW + 18 * DAY)))
add("account/billing.html", "free with old invoices", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                           state=make_state("free", used=25)))
add("account/billing.html", "legacy plan", base("/account/billing", user=MEMBER, plans=PLANS, invoices_url_available=True,
                                                state=make_state("studio_legacy", used=100)))

add("account/devices.html", "two devices", base("/account/devices", user=MEMBER, devices=[
    {"id": "d0c5a1e2-5b6f-4a7c-8d9e-0f1a2b3c4d5e", "label": "Chrome on macOS (work laptop)", "extension_version": "1.0.2",
     "created_at": NOW - 40 * DAY, "last_used_at": NOW - 3600},
    {"id": "7a8b9c0d-1e2f-4a3b-9c4d-5e6f7a8b9c0d", "label": "Chrome on Windows (home)", "extension_version": "1.0.1",
     "created_at": NOW - 2 * DAY, "last_used_at": None},
]))
add("account/devices.html", "none", base("/account/devices", user=MEMBER, devices=[]))

def sign_in(*, has_password: bool = True, google_available: bool = False, google: dict | None = None,
            recent: bool = False, back: str = "/account/security") -> dict:
    """account/routes.py sign_in_context."""
    return {"email": "casey@example.com", "has_password": has_password, "google_available": google_available,
            "google": google, "recent": recent, "reauth_url": "/auth/google?intent=reauth&next=" + back}


GOOGLE_LINK = {"email_masked": "c***@gmail.com", "connected_at": NOW - 9 * DAY}

FREE_PLAN = {"name": "Free", "is_paid": False, "period_end": None, "cancel_at": None}
PAID_PLAN = {"name": "Pro", "is_paid": True, "period_end": NOW + 29 * DAY, "cancel_at": None}


def add_data(label: str, context: dict) -> None:
    context.setdefault("plan", FREE_PLAN)
    add("account/data.html", label, context)


add("account/data.html", "default", base("/account/data", user=MEMBER, errors={}, sign_in=sign_in(back="/account/data"),
                                         plan=FREE_PLAN))
add("account/data.html", "paid plan", base("/account/data", user=MEMBER, errors={}, sign_in=sign_in(back="/account/data"),
                                           plan=PAID_PLAN))
add("account/data.html", "plan unknown", base("/account/data", user=MEMBER, errors={}, sign_in=sign_in(back="/account/data"),
                                              plan=None))
add("goodbye.html", "free", base("/goodbye", email="casey@example.com", plan_name=None, pending=False))
add("goodbye.html", "paid, still finishing", base("/goodbye", email="casey@example.com", plan_name="Pro", pending=True))
add("account/data.html", "delete errors", base("/account/data", user=MEMBER, sign_in=sign_in(back="/account/data"), plan=FREE_PLAN, errors={
    "delete": "Your account was not deleted.", "delete_password": "Your password is incorrect.",
    "delete_confirmation": "Type DELETE in capital letters."}))
add("account/data.html", "export failed", base("/account/data", user=MEMBER, sign_in=sign_in(back="/account/data"), plan=FREE_PLAN, errors={
    "export": "We could not prepare your export. Try again in a few minutes."}))
add("account/data.html", "Google only, just signed in", base(
    "/account/data", user=MEMBER, errors={}, plan=FREE_PLAN,
    sign_in=sign_in(has_password=False, google_available=True, google=GOOGLE_LINK, recent=True, back="/account/data")))
add("account/data.html", "Google only, confirm first", base(
    "/account/data", user=MEMBER, plan=FREE_PLAN, errors={"delete": "For your security, confirm it is you with Google first."},
    sign_in=sign_in(has_password=False, google_available=True, google=GOOGLE_LINK, back="/account/data")))

SESSIONS = [
    {"id": "s1", "current": True, "created_at": NOW - 5 * DAY, "last_seen_at": NOW - 60, "user_agent_summary": "Chrome 128 on macOS"},
    {"id": "s2", "current": False, "created_at": NOW - 20 * DAY, "last_seen_at": NOW - 3 * DAY, "user_agent_summary": "Chrome 127 on Windows"},
]
add("account/security.html", "default", base("/account/security", user=MEMBER, sessions=SESSIONS, errors={},
                                              sign_in=sign_in()))
add("account/security.html", "errors", base("/account/security", user=MEMBER, sessions=SESSIONS, sign_in=sign_in(), errors={
    "password": "Your password was not changed.", "current_password": "Your password is incorrect.",
    "new_password": "Use at least 12 characters.", "new_email": "Enter a valid email address."}))
add("account/security.html", "Google offered", base("/account/security", user=MEMBER, sessions=SESSIONS, errors={},
                                                     sign_in=sign_in(google_available=True)))
add("account/security.html", "Google and a password", base(
    "/account/security", user=MEMBER, sessions=SESSIONS, errors={},
    sign_in=sign_in(google_available=True, google=GOOGLE_LINK)))
add("account/security.html", "Google disconnect refused", base(
    "/account/security", user=MEMBER, sessions=SESSIONS,
    errors={"google_password": "Your password is incorrect."},
    sign_in=sign_in(google_available=True, google=GOOGLE_LINK)))
add("account/security.html", "Google only, just signed in", base(
    "/account/security", user=MEMBER, sessions=SESSIONS, errors={},
    sign_in=sign_in(has_password=False, google_available=True, google=GOOGLE_LINK, recent=True)))
add("account/security.html", "Google only, confirm first", base(
    "/account/security", user=MEMBER, sessions=SESSIONS,
    errors={"password": "For your security, confirm it is you with Google first."},
    sign_in=sign_in(has_password=False, google_available=True, google=GOOGLE_LINK)))

# ---------------------------------------------------------------- connect (devices/routes.py), billing, extension
add("connect/approve.html", "signed in", base("/connect", user=MEMBER, extension_id=EXTENSION_ID,
                                              device_label="Chrome on macOS (work laptop)",
                                              connect_request_id="cr_5b1f0e", connect_state="st_sample_c0ffee"))
add("connect/error.html", "expired", base("/connect/approve", user=MEMBER, extension_id=None, device_label=None,
                                          error="This connection request expired or was already used. Start connecting again from the extension."))
add("connect/error.html", "malformed, known extension", base(
    "/connect", extension_id=EXTENSION_ID, device_label="Chrome on macOS",
    error="The connection request is malformed. Start connecting again from the extension."))

add("billing/return.html", "waiting for webhook", base("/billing/return", user=MEMBER, session_status="complete",
                                                       state=make_state("free", used=2)))
add("billing/return.html", "open", base("/billing/return", user=MEMBER, session_status="open", state=make_state("free", used=2)))
add("billing/return.html", "complete", base("/billing/return", user=MEMBER, session_status="complete",
                                            state=make_state("starter", used=0)))
add("billing/return.html", "payment not finished", base(
    "/billing/return", user=MEMBER, session_status="complete",
    state=make_state("free", used=2, payment_problem=True, payment_issue=FIRST_PAYMENT)))
add("billing/return.html", "payment not finished, unknown plan", base(
    "/billing/return", user=MEMBER, session_status="complete",
    state=make_state("free", used=2, payment_problem=True, payment_issue={"kind": "first_payment", "plan_code": None, "plan_name": None})))
add("billing/return.html", "waiting, older gateway without payment_issue", base(
    "/billing/return", user=MEMBER, session_status="complete", state={
        **make_state("free", used=2),
        "plan": {k: v for k, v in make_state("free", used=2)["plan"].items() if k != "payment_issue"}}))
add("billing/return.html", "expired", base("/billing/return", user=MEMBER, session_status="expired", state=make_state("free")))
add("billing/return.html", "not found", base("/billing/return", user=MEMBER, session_status="not_found", state=make_state("free")))

add("extension_removed.html", "signed out", base("/extension/removed"))
add("extension_removed.html", "store link", base("/extension/removed", user=MEMBER, install_url=INSTALL_URL))

# ---------------------------------------------------------------- the writing page (web/write.py)
# The disclosure's shape; the real text is web/write.py DISCLOSURE, which the gateway's own tests render.
WRITE_DISCLOSURE = {
    "title": "Before your first draft",
    "intro": "Starling writes drafts with its hosted AI, so what you give it here is sent to Starling's servers. "
             "Nothing is sent until you agree.",
    "sections": [
        {"heading": "What Starling sends", "items": [
            "The post you paste or share: its words, the author's name or handle if you add it, and its link.",
            "Posts of your own that you add as writing samples."]},
        {"heading": "Who receives it", "items": ["Starling's servers, operated by the service owner.",
                                                 "Starling's AI provider, which writes the drafts."]},
    ],
    "agree": "Agree and continue",
    "decline": "Not now",
}
WRITE_CONSENT = {"given": True, "granted_at": NOW - 2 * DAY, "version": "2026-10-30.1"}
NO_WRITE_CONSENT = {"given": False, "granted_at": None, "version": "2026-10-30.1"}
NO_PREFILL = {"text": "", "link": None, "shared": False}


def write_page(label: str, *, consent: dict = WRITE_CONSENT, prefill: dict = NO_PREFILL,
               next_path: str = "/write", **state) -> None:
    add("write/index.html", label, base("/write", user=MEMBER, state=make_state(**state), consent=consent,
                                        disclosure=WRITE_DISCLOSURE, prefill=prefill, next_path=next_path))


write_page("ready to write", code="free", used=3)
write_page("first visit, consent needed", consent=NO_WRITE_CONSENT, code="free", used=0)
write_page("shared from the X app", consent=NO_WRITE_CONSENT, code="starter",
           prefill={"text": "https://x.com/dana_example/status/1851234567890123456?s=20",
                    "link": "https://x.com/dana_example/status/1851234567890123456", "shared": True},
           next_path="/write?text=https%3A%2F%2Fx.com%2Fdana_example%2Fstatus%2F1851234567890123456")
write_page("no drafts left", code="free", used=25)
write_page("drafting paused", code="pro", service_status="paused",
           service_message="Drafting is paused while we fix an upstream problem.")
write_page("email not confirmed", code="free", used=0, verified=False)
write_page("suspended", code="starter", account_status="suspended")
add("write/index.html", "production, consent given, store link", base(
    "/write", user=MEMBER, env="production", install_url=INSTALL_URL, state=make_state("starter"),
    consent=WRITE_CONSENT, disclosure=WRITE_DISCLOSURE, prefill=NO_PREFILL, next_path="/write"))

VOICE_LIMITS = {"max_chars": 2000, "min_words": 3, "max_items": 200}
SAMPLE_POSTS = [
    {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
     "text": "Shipping beats polishing.\n\nTen small releases taught us more than one big one.",
     "added_at": NOW - DAY},
    {"id": "0f9e8d7c6b5a49382716051423324150", "text": "The best feedback I ever got was a bug report with a screenshot.",
     "added_at": None},
]


def voice_page(label: str, *, consent: dict = WRITE_CONSENT, samples=SAMPLE_POSTS, errors=None, form=None,
               unavailable=None, env: str = "development", **state) -> None:
    add("write/voice.html", label, base("/write/voice", user=MEMBER, env=env, state=make_state(**state),
                                        consent=consent, disclosure=WRITE_DISCLOSURE, samples=samples,
                                        limits=VOICE_LIMITS, errors=errors or {}, form=form or {},
                                        unavailable=unavailable, next_path="/write/voice"))


voice_page("two samples")
voice_page("no samples yet", samples=[])
voice_page("consent needed", consent=NO_WRITE_CONSENT, samples=None)
voice_page("too short", errors={"text": "A voice example needs at least 3 words — anything shorter teaches the model nothing."},
           form={"text": "so true"})
voice_page("engine unavailable", samples=None,
           unavailable="Starling can't load your writing samples right now. Try again in a moment.")
voice_page("save failed", errors={"form": "Starling couldn't save that right now. Try again in a moment."},
           form={"text": "A post I wrote last week about release notes."})
voice_page("email not confirmed", samples=None, verified=False)
voice_page("production", env="production")

add("offline.html", "signed out", base("/offline"))
add("offline.html", "production", base("/offline", env="production"))

# ---------------------------------------------------------------- admin identity (auth/routes.py)
add("admin/totp.html", "enrollment", base("/admin/totp", user=OWNER, errors={}, secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                                         otpauth_uri="otpauth://totp/Starling:o%2A%2A%2A%40starling.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Starling"))
add("admin/totp.html", "enrollment error", base("/admin/totp", user=OWNER, errors={"form": "That code is incorrect."},
                                               secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                                               otpauth_uri="otpauth://totp/Starling:o%2A%2A%2A%40starling.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Starling"))
add("admin/elevate.html", "default", base("/admin/elevate", user=OWNER, errors={}, next="/admin"))
add("admin/elevate.html", "wrong code", base("/admin/elevate", user=OWNER, errors={"form": "That code is incorrect."},
                                            next="/admin/customers"))

# ---------------------------------------------------------------- admin dashboard (backend admin/metrics.py and admin/customers.py)
GIB = 1024 ** 3
METRICS = {
    "generated_at": NOW - 120,
    "service": {"status": "ok", "message": None, "owner_paused": False, "free_paused": False},
    "alerts": [],
    "paid_accounts": 41, "free_accounts": 1204, "mrr_cents": 50_900, "currency": "usd",
    "subscribers": [
        {"plan_code": "starter", "plan_name": "Starter", "active": 30, "past_due": 2, "suspended": 0, "canceling": 3},
        {"plan_code": "pro", "plan_name": "Pro", "active": 11, "past_due": 0, "suspended": 1, "canceling": 1},
    ],
    "credits": {"today": 318, "last_7d": 2140, "last_30d": 9025},
    "ai": {"calls_today": 1022, "cost_today_micros": 6_420_000, "daily_budget_micros": 20_000_000,
           "cost_month_micros": 171_300_000, "monthly_budget_micros": 300_000_000, "calls_30d": 29_310,
           "released_with_spend_30d": 57},
    "failures": {"window_label": "last 24 hours", "requests": 1340, "failed": 22, "rate_pct": 1.64,
                 "top_codes": [{"code": "ai_unavailable", "count": 12}, {"code": "rate_limited", "count": 7},
                               {"code": "busy", "count": 3}]},
    "webhooks": {"received_24h": 64, "pending": 0, "failed": 0, "oldest_pending_at": None, "last_processed_at": NOW - 900},
    "backup": {"at": NOW - 7 * 3600, "ok": True, "archive": "starling-2026-09-14T030000Z.tar.age",
               "size_bytes": 148 * 1024 * 1024, "verified": True, "error": None},
    "disk": {"path": "/data", "total_bytes": 60 * GIB, "free_bytes": 41 * GIB, "free_pct": 68.33},
}
METRICS_TROUBLE = copy.deepcopy(METRICS)
METRICS_TROUBLE["service"] = {"status": "paused", "message": "Daily AI budget reached.", "owner_paused": False,
                              "free_paused": False}
METRICS_TROUBLE["alerts"] = [
    {"level": "error", "text": "The AI provider rejected the primary API key (HTTP 401); requests were retried with "
                               "the secondary key.", "created_at": NOW - 3600, "key": "alert:ai_key_rejected"},
    {"level": "warning", "text": "Billing: a price id is not in plans.toml. Reference price_Sample9. Seen 3 times.",
     "created_at": NOW - 5400, "key": "alert:billing:price_missing:price_Sample9"},
]
METRICS_TROUBLE["webhooks"] = {"received_24h": 70, "pending": 4, "failed": 2, "oldest_pending_at": NOW - 7200, "last_processed_at": NOW - 5400}
METRICS_TROUBLE["ai"]["cost_today_micros"] = 20_000_000
METRICS_TROUBLE["backup"] = {"at": NOW - 30 * 3600, "ok": False, "archive": None, "size_bytes": None,
                             "verified": False, "error": "the passphrase is not set"}
METRICS_TROUBLE["disk"] = {"path": "/data", "total_bytes": 60 * GIB, "free_bytes": 2 * GIB, "free_pct": 3.33}
METRICS_FREE_PAUSED = copy.deepcopy(METRICS)
METRICS_FREE_PAUSED["service"] = {"status": "ok", "message": None, "owner_paused": False, "free_paused": True}
METRICS_FREE_PAUSED["backup"] = None
METRICS_FREE_PAUSED["disk"] = None

add("admin/overview.html", "healthy", base("/admin", user=OWNER, admin=ELEVATED, metrics=METRICS))
add("admin/overview.html", "paused, no admin block", base("/admin", user=OWNER, metrics=METRICS_TROUBLE))
add("admin/overview.html", "free accounts paused, nothing measured",
    base("/admin", user=OWNER, admin=ELEVATED, metrics=METRICS_FREE_PAUSED))

CUSTOMERS = [
    {"id": ACCOUNT_ID, "email": "customer-0042@example.com", "plan_name": "Starter", "plan_status": "active",
     "account_status": "active", "used": 203, "allowance": 250, "created_at": NOW - 90 * DAY, "last_active_at": NOW - 3600},
    {"id": "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", "email": "customer-0043@example.com", "plan_name": "Free",
     "plan_status": "free", "account_status": "suspended", "used": 5, "allowance": 25, "created_at": NOW - 4 * DAY,
     "last_active_at": None},
]
add("admin/customers.html", "results", base("/admin/customers", user=OWNER, admin=ELEVATED, query="example.com",
                                           customers=CUSTOMERS, page={"number": 2, "has_prev": True, "has_next": True}))
add("admin/customers.html", "no match", base("/admin/customers", user=OWNER, query="nobody",
                                            customers=[], page={"number": 1, "has_prev": False, "has_next": False}))

CUSTOMER = {
    "id": ACCOUNT_ID, "email": "customer-0042@example.com", "tenant_seq": 42, "created_at": NOW - 90 * DAY,
    "email_verified": True, "account_status": "active", "is_admin": False, "is_self": False, "dispute_open": False,
    "stripe_customer_id": "cus_Sample42", "devices_active": 2,
    "subscription": {"id": "sub_Sample42", "status": "active", "plan_name": "Starter",
                     "current_period_end": NOW + 18 * DAY, "cancel_at": None},
}
COST = {"period_cost_micros": 1_950_000, "ceiling_micros": 4_500_000, "ai_calls": 611, "released_with_spend": 3}
RECENT_ERRORS = [{"at": NOW - 1800, "code": "ai_unavailable", "route": "/api/v1/assist/stream", "count": 2}]
LEDGER = [{"created_at": NOW - 6 * DAY, "kind": "admin_grant", "amount": 20, "actor": "owner@starling.example",
           "reason": "Outage credit"}]
add("admin/customer.html", "active", base(f"/admin/customers/{ACCOUNT_ID}", user=OWNER, admin=ELEVATED, errors={},
                                         customer=CUSTOMER, state=make_state("starter", used=203), cost=COST,
                                         recent_errors=RECENT_ERRORS, ledger=LEDGER))
SUSPENDED = {**CUSTOMER, "account_status": "suspended", "dispute_open": True, "stripe_customer_id": None,
             "subscription": None, "devices_active": 0}
add("admin/customer.html", "suspended with an open dispute", base(
    f"/admin/customers/{ACCOUNT_ID}", user=OWNER, errors={},
    customer=SUSPENDED, state=make_state("free", used=5, account_status="suspended", plan_status="suspended"),
    cost={**COST, "period_cost_micros": 0, "ceiling_micros": 300_000}, recent_errors=[], ledger=[]))
add("admin/customer.html", "the owner's own account", base(
    f"/admin/customers/{ACCOUNT_ID}", user=OWNER, admin=ELEVATED, errors={},
    customer={**CUSTOMER, "is_admin": True, "is_self": True}, state=make_state("pro", used=40), cost=COST,
    recent_errors=RECENT_ERRORS, ledger=[]))

FAILURE_TOTALS = {"requests": 1340, "failed": 22, "rate_pct": 1.64}
add("admin/failures.html", "some failures", base("/admin/failures", user=OWNER, admin=ELEVATED, window_label="last 24 hours",
                                                totals=FAILURE_TOTALS,
                                                summary=[{"code": "ai_unavailable", "count": 12, "rate_pct": 0.9}],
                                                failures=[{"at": NOW - 600, "account_id": ACCOUNT_ID, "route": "/api/v1/assist",
                                                           "mode": "context", "code": "ai_unavailable", "http_status": 503,
                                                           "count": 2},
                                                          {"at": NOW - 900, "account_id": "", "route": "/api/v1/session",
                                                           "mode": "", "code": "rate_limited", "http_status": 429,
                                                           "count": 7}]))
add("admin/failures.html", "none", base("/admin/failures", user=OWNER, window_label="last 24 hours",
                                        totals={"requests": 0, "failed": 0, "rate_pct": 0.0}, summary=[], failures=[]))

EVENTS = [
    {"event_id": "evt_Sample001", "type": "invoice.paid", "status": "processed", "attempts": 1, "last_error": None,
     "received_at": NOW - 900, "processed_at": NOW - 899, "livemode": True},
    {"event_id": "evt_Sample002", "type": "customer.subscription.updated", "status": "failed", "attempts": 3,
     "last_error": "subscription lookup timed out", "received_at": NOW - 7200, "processed_at": None, "livemode": False},
]
add("admin/webhooks.html", "mixed", base("/admin/webhooks", user=OWNER, admin=ELEVATED, status_filter="all", events=EVENTS,
                                        summary={"received": 0, "processed": 63, "failed": 1, "oldest_pending_at": None,
                                                 "last_processed_at": NOW - 899}))
add("admin/webhooks.html", "filtered empty", base("/admin/webhooks", user=OWNER, status_filter="failed",
                                                 events=[], summary={"received": 2, "processed": 10, "failed": 0,
                                                                     "oldest_pending_at": NOW - 300, "last_processed_at": None}))

add("admin/audit.html", "entries", base("/admin/audit", user=OWNER, admin=ELEVATED, page={"number": 1, "has_prev": False, "has_next": True},
                                       entries=[{"created_at": NOW - 6 * DAY, "admin_email": "owner@starling.example",
                                                 "action": "grant_credits", "target_account_id": ACCOUNT_ID,
                                                 "params_summary": "credits=20", "ip_prefix": "203.0.113.0/24"},
                                                {"created_at": NOW - 7 * DAY, "admin_email": "owner@starling.example",
                                                 "action": "ai_check", "target_account_id": None, "params_summary": "",
                                                 "ip_prefix": "203.0.113.0/24"}]))
add("admin/audit.html", "empty", base("/admin/audit", user=OWNER, entries=[],
                                     page={"number": 1, "has_prev": False, "has_next": False}))

AI = {
    "base_url_host": "api.openai.com", "reasoning_effort": "none",
    "services": {"ok": True, "error_code": None},
    "keys": [{"slot": "primary", "configured": True, "masked": "…Q7wX", "in_use": True},
             {"slot": "secondary", "configured": False, "masked": None, "in_use": False}],
    "models": [{"lane": "quality", "model": "gpt-5.6-luna"}, {"lane": "fast", "model": "gpt-5.6-luna"},
               {"lane": "vision", "model": "gpt-5.6-luna"}],
    "budgets": {"cost_today_micros": 6_420_000, "daily_budget_micros": 20_000_000,
                "cost_month_micros": 171_300_000, "monthly_budget_micros": 300_000_000,
                "day_tripped": False, "month_tripped": False, "free_tripped": False},
    "pause": {"paused": False, "message": None, "since": None},
    "service": {"status": "ok", "message": None, "owner_paused": False, "free_paused": False},
    "last_test": {"at": NOW - 3600, "ok": True, "latency_ms": 812, "model": "gpt-5.6-luna", "error_code": None},
    "search": {"enabled": True, "model": "gpt-5.6-luna", "per_account_day": 20, "searches_today": 14,
               "cost_today_micros": 171_000, "price_per_1k_micros": 10_000_000},
}
add("admin/ai.html", "test passed", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai=AI))
add("admin/ai.html", "web search off", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai={
    **AI, "search": {**AI["search"], "enabled": False, "searches_today": 0, "cost_today_micros": 0}}))
add("admin/ai.html", "web search without a price", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai={
    **AI, "search": {**AI["search"], "searches_today": 1, "price_per_1k_micros": None}}))
add("admin/ai.html", "test failed, services unreachable", base(
    "/admin/ai", user=OWNER, errors={},
    ai={**AI, "services": {"ok": False, "error_code": "services_unreachable"}, "keys": [],
        "last_test": {"at": NOW - 60, "ok": False, "latency_ms": 0, "model": "gpt-5.6-luna", "error_code": "ai_auth"}}))
add("admin/ai.html", "never tested", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai={**AI, "last_test": None}))
AI_PAUSED = copy.deepcopy(AI)
AI_PAUSED["pause"] = {"paused": True, "message": "Drafting is paused while we fix an upstream problem.",
                      "since": NOW - 1800}
AI_PAUSED["service"] = {"status": "paused", "message": AI_PAUSED["pause"]["message"], "owner_paused": True,
                        "free_paused": False}
add("admin/ai.html", "paused by the owner", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai=AI_PAUSED))
AI_FREE_PAUSED = copy.deepcopy(AI)
AI_FREE_PAUSED["budgets"].update({"cost_today_micros": 18_000_000, "free_tripped": True})
AI_FREE_PAUSED["service"] = {"status": "ok", "message": None, "owner_paused": False, "free_paused": True}
add("admin/ai.html", "paused for free accounts", base("/admin/ai", user=OWNER, admin=ELEVATED, errors={}, ai=AI_FREE_PAUSED))

# ---------------------------------------------------------------- errors (not wired yet: PageError renders inline HTML)
add("errors/404.html", "signed out", base("/no-such-page"))
add("errors/500.html", "with reference", base("/account", user=MEMBER, request_id="req_7c4e2b19"))
add("errors/500.html", "minimal", base("/"))
