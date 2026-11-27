"""Website template gate. Run from the repository root: python -m pytest web/tests

Every template is rendered with the sample contexts under StrictUndefined and checked with the
page checks in scripts/check_site.py. Content tests cover what a generic check cannot: pricing
shows the plans from context, the account overview shows exactly the numbers in state.usage, the
legal banner and placeholders follow the environment, and the colour tokens meet WCAG contrast.
Negative controls prove the checker fails a broken template and a broken server, and a contract
test keeps the templates in step with what the gateway's web/render.py passes.
"""
from __future__ import annotations

import ast
import copy
import http.server
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import jinja2
import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
REPO = WEB_DIR.parent
CHECKER = REPO / "scripts" / "check_site.py"
GATEWAY_RENDER = REPO / "backend" / "server" / "starling_server" / "web" / "render.py"
GATEWAY_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
               "connect-src 'self'; manifest-src 'self'; form-action 'self' https://checkout.stripe.com "
               "https://billing.stripe.com https://invoice.stripe.com; frame-ancestors 'none'; base-uri 'none'; "
               "object-src 'none'")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_site = _load("starling_check_site_under_test", CHECKER)
templating = _load("starling_web_templating_under_test", WEB_DIR / "templating.py")
samples = _load("starling_web_samples_under_test", Path(__file__).with_name("sample_contexts.py"))
ENV = templating.create_environment(WEB_DIR, strict=True)


def render(template: str, context: dict) -> str:
    return ENV.get_template(template).render(**context)


def samples_for(template: str) -> dict[str, dict]:
    return {label: context for name, label, context in samples.SAMPLES if name == template}


def text_of(html: str) -> str:
    return check_site.ParsedPage(html).text()


def forms_of(html: str) -> list[tuple[str, dict[str, str]]]:
    page = check_site.ParsedPage(html)
    return [(form.attrs.get("action", ""), {e.attrs.get("name"): e.attrs.get("value", "") for e in page.inside(form) if e.tag == "input"})
            for form in page.elements if form.tag == "form"]


# --------------------------------------------------------------------------- every template, every sample

def test_every_template_has_a_sample():
    names = set(check_site.template_names(WEB_DIR))
    covered = {name for name, _, _ in samples.SAMPLES}
    assert names - covered == set()
    assert covered - names == set()
    assert len(names) >= 40


@pytest.mark.parametrize(("template", "label", "context"), samples.SAMPLES,
                         ids=[f"{name} [{label}]" for name, label, _ in samples.SAMPLES])
def test_page_passes_all_checks(template, label, context):
    html = render(template, context)
    assert check_site.check_page(html, **check_site.page_options_for_template(template, context)) == []


def test_missing_context_key_fails_loudly():
    context = copy.deepcopy(samples_for("pricing.html")["signed out"])
    del context["plans"]
    with pytest.raises(jinja2.UndefinedError):
        render("pricing.html", context)


# --------------------------------------------------------------------------- the checker catches each rule

GOOD_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Sample</title>
<meta name="robots" content="noindex, nofollow"></head><body><a href="#main">Skip to content</a>
<main id="main" tabindex="-1"><h1>Title</h1><h2>Section</h2><p>Plain text.</p>
<form method="post" action="/login"><input type="hidden" name="csrf_token" value="token">
<div data-error-summary aria-live="assertive" tabindex="-1" hidden></div>
<label for="field-email">Email</label><input id="field-email" name="email" type="email" autocomplete="email">
<button type="submit">Sign in</button></form><img src="/static/img/logo.png" alt=""></main></body></html>"""

MUTATIONS = [
    ("inline script", ("</main>", "<script>alert(1)</script></main>"), "inline <script>"),
    ("style element", ("</head>", "<style>p{}</style></head>"), "<style> element"),
    ("style attribute", ("<p>Plain", '<p style="color:red">Plain'), "style attribute"),
    ("event handler", ('<button type="submit">', '<button type="submit" onclick="go()">'), "event handler"),
    ("second h1", ("<h2>Section</h2>", "<h1>Again</h1><h2>Section</h2>"), "exactly one <h1>"),
    ("skipped heading", ("<h2>Section</h2>", "<h3>Section</h3>"), "heading level skipped"),
    ("skip link not first", ('<a href="#main">Skip to content</a>', '<a href="/">Home</a><a href="#main">Skip</a>'), "skip link"),
    ("main without id", ('<main id="main" tabindex="-1">', "<main>"), '<main id="main">'),
    ("unlabelled field", ('<label for="field-email">Email</label>', ""), "has no label"),
    ("missing autocomplete", (' autocomplete="email"', ""), "no autocomplete"),
    ("missing alt", ('alt=""', ""), "no alt attribute"),
    ("missing csrf", ('<input type="hidden" name="csrf_token" value="token">', ""), "csrf_token"),
    ("missing error summary", ('<div data-error-summary aria-live="assertive" tabindex="-1" hidden></div>', ""), "aria-live"),
    ("orphan aria-invalid", (' autocomplete="email"', ' autocomplete="email" aria-invalid="true"'), "aria-invalid"),
    ("csrf on GET form", ('method="post"', 'method="get"'), "GET form must not carry csrf_token"),
    ("javascript URL", ("<p>Plain", '<a href="javascript:void(0)">x</a><p>Plain'), "javascript: URL"),
    ("off-origin resource", ('src="/static/img/logo.png"', 'src="https://cdn.example.com/logo.png"'), "off-origin resource"),
    ("missing noindex", ('<meta name="robots" content="noindex, nofollow">', ""), "noindex"),
    ("placeholder outside legal", ("Plain text.", "[[OPERATOR_COMPANY_NAME]]"), "outside legal pages"),
    ("local claim", ("Plain text.", "Nothing leaves your computer."), "banned wording"),
    ("testimonial", ("Plain text.", "Read our testimonials."), "banned wording"),
    ("user count", ("Plain text.", "Join 10,000 users today."), "banned wording"),
    ("guarantee", ("Plain text.", "Money-back guarantee."), "banned wording"),
    ("number one", ("Plain text.", "The #1 writing tool."), "banned wording"),
    ("urgency", ("Plain text.", "Limited time offer, act now."), "banned wording"),
]


def test_good_page_passes():
    assert check_site.check_page(GOOD_PAGE, kind="private", env="development", origin="https://starling.example") == []


@pytest.mark.parametrize(("name", "change", "expected"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_checker_catches_each_rule(name, change, expected):
    old, new = change
    assert old in GOOD_PAGE
    problems = check_site.check_page(GOOD_PAGE.replace(old, new, 1), kind="private", env="development",
                                     origin="https://starling.example")
    assert any(expected in problem for problem in problems), problems


def test_names_in_the_private_names_file_are_banned_wording(tmp_path, monkeypatch):
    """A retired brand or a person's name is nobody's business but the operator's, so the checker
    does not spell any out: it reads them from a file the repository never carries."""
    names = tmp_path / "names.json"
    rules = {"_note": "a comment", "brand": {"retired name": r"(?i)\bnorthwind\b"}}
    names.write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setattr(check_site, "RAW_PATTERNS", list(check_site.RAW_PATTERNS))
    assert check_site.load_private_names(str(names)) == 1
    page = GOOD_PAGE.replace("Plain text.", "By Northwind.", 1)
    problems = check_site.check_page(page, kind="private", env="development", origin="https://starling.example")
    assert any("banned wording (private name: retired name)" in problem for problem in problems), problems
    with pytest.raises(SystemExit):
        check_site.load_private_names(str(tmp_path / "missing.json"))


# --------------------------------------------------------------------------- pricing and plans

@pytest.mark.parametrize(("label", "context"), list(samples_for("pricing.html").items()))
def test_pricing_shows_every_plan_from_context(label, context):
    html = render("pricing.html", context)
    page = check_site.ParsedPage(html)
    text = page.text()
    card_names = [e.text.strip() for e in page.elements if e.tag == "h2" and any(
        page.elements[i].tag == "li" and "plan" in page.elements[i].attrs.get("class", "").split() for i in e.ancestors)]
    assert card_names == [plan["name"] for plan in context["plans"]]
    for plan in context["plans"]:
        assert plan["price_label"] in text
        assert f"{plan['monthly_credits']:,} {context['unit']['label']} a month" in text
        assert f"up to {plan['rate_per_day']:,} requests a day" in text
    assert samples.LEGACY_PLAN["name"] not in card_names


def test_plan_numbers_come_from_context_not_the_template():
    context = copy.deepcopy(samples_for("pricing.html")["signed out"])
    context["plans"][1].update(name="Starter Plus", monthly_credits=321, amount_cents=1100, price_label="$11", rate_per_day=77)
    for template in ("pricing.html", "index.html"):
        text = text_of(render(template, context))
        assert "Starter Plus" in text and "$11" in text and "321 drafts a month" in text
        assert "250 drafts" not in text and "$9 " not in text


def test_pricing_actions_follow_the_signed_in_plan():
    by_label = samples_for("pricing.html")
    free = {(action, fields.get("plan_code")) for action, fields in forms_of(render("pricing.html", by_label["free"]))}
    assert {("/billing/checkout", "starter"), ("/billing/checkout", "pro")} <= free
    starter = {(action, fields.get("plan_code")) for action, fields in forms_of(render("pricing.html", by_label["starter near limit"]))}
    assert ("/billing/upgrade", "pro") in starter and not any(a == "/billing/checkout" for a, _ in starter)
    pro = {(action, fields.get("plan_code")) for action, fields in forms_of(render("pricing.html", by_label["pro limit reached from extension"]))}
    assert ("/billing/downgrade", "starter") in pro
    signed_out = forms_of(render("pricing.html", by_label["signed out"]))
    assert [action for action, _ in signed_out] == []
    # A plan set to end changes only once it is kept (billing refuses "subscription_ending").
    cancelling = render("pricing.html", by_label["starter cancelling"])
    assert not any(a.startswith("/billing/") for a, _ in forms_of(cancelling))
    assert "Keep Starter to change plans" in cancelling
    assert "Cancel to move to" not in cancelling and "Your account moves here on" in cancelling


# --------------------------------------------------------------------------- account pages

@pytest.mark.parametrize(("label", "context"), list(samples_for("account/overview.html").items()))
def test_account_overview_numbers_equal_state_usage(label, context):
    state, usage = context["state"], context["state"]["usage"]
    page = check_site.ParsedPage(render("account/overview.html", context))
    shown = {e.attrs["data-usage"]: e for e in page.elements if "data-usage" in e.attrs}
    as_int = lambda element: int(element.text.strip().replace(",", ""))
    assert as_int(shown["used"]) == usage["used"]
    assert as_int(shown["remaining"]) == usage["remaining"]
    assert as_int(shown["allowance"]) == usage["allowance"]
    assert ("reserved" in shown) == (usage["reserved"] > 0)
    if usage["reserved"]:
        assert as_int(shown["reserved"]) == usage["reserved"]
    assert shown["resets_at"].attrs["data-value"] == str(usage["resets_at"])
    assert shown["resets_at"].text.strip() == templating.format_date(usage["resets_at"])
    meter = next(e for e in page.elements if e.attrs.get("role") == "meter")
    assert meter.attrs["aria-valuemax"] == str(usage["allowance"])
    assert meter.attrs["aria-valuenow"] == str(min(usage["used"] + usage["reserved"], usage["allowance"]))
    assert meter.attrs["aria-valuetext"].endswith(f"{usage['remaining']:,} remaining")
    notices = {e.attrs["data-usage-notice"] for e in page.elements if "data-usage-notice" in e.attrs}
    expected = ({"limit"} if usage["remaining"] <= 0 else {"fair-use"} if usage["fair_use_blocked"]
                else {"near-limit"} if usage["near_limit"] else set())
    assert notices == expected
    text = page.text()
    # An unconfirmed account is told how to confirm: in the Get started checklist on a first visit,
    # in a notice after that.
    assert ("Enter the code" in text) == (not state["account"]["email_verified"])
    assert ("There is a problem with your payment" in text) == (state["plan"]["payment_problem"] or state["plan"]["status"] == "past_due")


def test_a_payments_rehearsal_is_said_on_the_page_and_never_indexed():
    """Production on Stripe's sandbox (STRIPE_ALLOW_TEST_MODE): nobody may mistake it for the real thing."""
    by_label = samples_for("pricing.html")
    rehearsal = render("pricing.html", by_label["payments rehearsal"])
    assert "data-payments-test-mode" in rehearsal
    assert "Payments are in test mode." in text_of(rehearsal) and "no real charges are made" in text_of(rehearsal)
    assert '<meta name="robots" content="noindex, nofollow">' in rehearsal, "production, but not to be indexed"

    production = dict(by_label["payments rehearsal"], site={k: v for k, v in by_label["payments rehearsal"]["site"].items()
                                                            if k != "payments_test_mode"})
    live = render("pricing.html", production)
    assert "data-payments-test-mode" not in live and "Payments are in test mode" not in text_of(live)
    assert '<meta name="robots" content="index, follow">' in live
    assert "data-payments-test-mode" not in render("pricing.html", by_label["signed out"])


def test_what_customers_are_told_about_their_data_is_what_the_service_does():
    """Found by a review of the site's claims: four statements to customers that the code did not keep.

    Deletion cancels the subscription AT ONCE with no refund of the unused part, and leaves nightly
    encrypted backups untouched for up to about three months, while the pages said "stops your
    subscription from renewing" and "permanently deletes"; the site set cookies and no page said so;
    and the terms require 18 while sign-up never asked. The wording is pinned here so that a
    later edit cannot quietly make it untrue again.
    """
    def page(template: str) -> str:
        label, context = next(iter(samples_for(template).items()))
        return text_of(render(template, context))

    paid_data_page = text_of(render("account/data.html", samples_for("account/data.html")["paid plan"]))
    for template, told in (("docs/privacy-and-data.html", page("docs/privacy-and-data.html")),
                           ("account/data.html", paid_data_page)):
        assert "not refunded" in told, template
        assert "backups" in told.lower() and "three months" in told, template
        assert "stops your subscription from renewing" not in told, template
    assert "permanently deletes your writing samples" not in page("docs/privacy-and-data.html")

    policy = page("legal/privacy.html")
    assert "Cookies" in policy and "only cookies it needs to work" in policy
    # The sign-up cookie holds the typed address and a random value for up to 24 hours (auth/routes.py
    # signup_submit): the policy says exactly that, not the "masked address for an hour" it once was.
    assert "for up to 24 hours" in policy and "a random value" in policy
    assert "no analytics, advertising or third-party cookies" in policy
    assert "kept for up to about three months" in policy

    assert "at least 18 years old" in page("auth/signup.html"), "the age the terms require is confirmed where the account is made"
    terms = page("legal/terms.html")
    assert "Each person may hold one Free account" not in terms, "a rule, not a claim that it is enforced"
    assert "You may not create extra accounts" in terms
    assert "before a new provider starts" not in page("legal/subprocessors.html")


def test_billing_actions_follow_plan_state():
    by_label = samples_for("account/billing.html")
    free = forms_of(render("account/billing.html", by_label["free"]))
    assert {("/billing/checkout", "starter"), ("/billing/checkout", "pro")} <= {(a, f.get("plan_code")) for a, f in free}
    assert not any(a == "/billing/portal" for a, _ in free)

    scheduled_html = render("account/billing.html", by_label["pro scheduled downgrade"])
    scheduled = forms_of(scheduled_html)
    assert any(a == "/billing/downgrade/undo" for a, _ in scheduled)
    assert not any(a == "/billing/downgrade" and f.get("plan_code") == "starter" for a, f in scheduled)
    assert "You move to Starter on" in text_of(scheduled_html)

    past_due_html = render("account/billing.html", by_label["starter past due"])
    past_due = forms_of(past_due_html)
    assert any(a == "/billing/portal" and f.get("flow") == "payment_method_update" for a, f in past_due)
    assert ("/billing/upgrade", "pro") in {(a, f.get("plan_code")) for a, f in past_due}
    assert "Your last payment did not go through" in text_of(past_due_html)

    # A FIRST payment that failed leaves the customer on the free plan. Found by the browser
    # journey that declined a card: the renewal wording told a free customer to update their
    # payment method "to keep Free" and that they "keep paid access", neither of which is true.
    #
    # The button matters as much as the words. Saving a card in the Customer Portal does not pay an
    # invoice that is already open, and Stripe does not retry a first payment by itself, so the
    # notice must post to /billing/checkout, which sends the customer to that invoice's own page.
    first_failed_html = render("account/billing.html", by_label["free, first payment failed"])
    first_failed = text_of(first_failed_html)
    first_forms = forms_of(first_failed_html)
    assert "Your Starter payment is not finished" in first_failed
    assert "You have not been charged, and you are still on Free" in first_failed
    assert "the unfinished payment is cancelled" in first_failed, "choosing another plan is said out loud"
    assert "Finish paying for Starter" in first_failed
    for untrue in ("to keep Free", "keep paid access", "tries the payment again", "Update payment method"):
        assert untrue not in first_failed, untrue
    assert 'data-payment-notice="first"' in first_failed_html
    # One way to pay for the unfinished plan: the notice's button. The plan list says it is waiting
    # instead of offering a second "Choose Starter" next to "Finish paying for Starter".
    assert [f.get("plan_code") for a, f in first_forms if a == "/billing/checkout"].count("starter") == 1
    assert "Payment not finished (see above)" in first_failed
    assert not any(f.get("flow") == "payment_method_update" for _, f in first_forms)
    assert ("/billing/checkout", "pro") in {(a, f.get("plan_code")) for a, f in first_forms}

    # The plan could not be named (its price maps to no plan): no promise, and Stripe's own page.
    unknown_html = render("account/billing.html", by_label["free, first payment failed, unknown plan"])
    unknown = text_of(unknown_html)
    assert "Your payment is not finished" in unknown and "Finish paying for" not in unknown
    assert "the unfinished payment is cancelled" not in unknown
    assert any(a == "/billing/portal" for a, _ in forms_of(unknown_html))

    # A gateway that does not send `payment_issue` yet still gets true words for a free customer.
    older = text_of(render("account/billing.html", by_label["free, first payment failed, older gateway"]))
    assert "Your payment is not finished" in older and "keep paid access" not in older

    # A renewal that failed and outlived the grace window: the customer HAD the plan and is on Free now.
    lapsed_html = render("account/billing.html", by_label["free, renewal lapsed"])
    lapsed = text_of(lapsed_html)
    assert 'data-payment-notice="lapsed"' in lapsed_html
    assert "Your Starter payment did not go through" in lapsed
    assert "your account is on Free for now" in lapsed and "Starter returns as soon as the payment succeeds" in lapsed
    assert "You have not been charged" not in lapsed and "keep paid access" not in lapsed
    assert any(a == "/billing/portal" and f.get("flow") == "payment_method_update" for a, f in forms_of(lapsed_html))

    assert 'data-payment-notice="renewal"' in past_due_html and "keep paid access" in text_of(past_due_html)

    overview = samples_for("account/overview.html")
    first_overview_html = render("account/overview.html", overview["free, first payment failed"])
    assert 'data-payment-notice="first"' in first_overview_html
    assert "you have not been charged and you are still on Free" in text_of(first_overview_html)
    assert not any(a == "/billing/portal" for a, _ in forms_of(first_overview_html))
    lapsed_overview_html = render("account/overview.html", overview["free, renewal lapsed"])
    assert 'data-payment-notice="lapsed"' in lapsed_overview_html
    assert "on Free for now" in text_of(lapsed_overview_html)
    assert any(f.get("flow") == "payment_method_update" for _, f in forms_of(lapsed_overview_html))

    cancelling_html = render("account/billing.html", by_label["starter cancelling"])
    assert "Keep Starter" in text_of(cancelling_html)
    assert any(a == "/billing/resume" for a, _ in forms_of(cancelling_html)), "kept in one click, no trip to Stripe"
    assert not any(f.get("flow") == "subscription_cancel" for _, f in forms_of(cancelling_html))
    assert any(f.get("flow") == "subscription_cancel" for _, f in past_due)


def test_the_return_page_never_waits_for_a_payment_that_will_not_arrive():
    """Found in a real browser: after a declined card the page said "Confirming your subscription"
    and polled for two minutes. Waiting cannot help; the page has to say so and offer the way out."""
    by_label = samples_for("billing/return.html")

    unfinished_html = render("billing/return.html", by_label["payment not finished"])
    unfinished = text_of(unfinished_html)
    assert "Your payment is not finished" in unfinished
    assert "you have not been charged and you are still on Free" in unfinished
    assert "Confirming your subscription" not in unfinished
    assert "data-billing-poll" not in unfinished_html and "billing-return.js" not in unfinished_html
    assert 'http-equiv="refresh"' not in unfinished_html, "no reload loop without JavaScript either"
    assert ("/billing/checkout", "starter") in {(a, f.get("plan_code")) for a, f in forms_of(unfinished_html)}
    assert 'href="/account/billing"' in unfinished_html

    unknown_html = render("billing/return.html", by_label["payment not finished, unknown plan"])
    assert "Your payment is not finished" in text_of(unknown_html)
    assert not forms_of(unknown_html) or all(a != "/billing/checkout" for a, _ in forms_of(unknown_html))
    assert 'href="/account/billing"' in unknown_html

    # Still waiting when there is genuinely something to wait for, with or without the newer field.
    for label in ("waiting for webhook", "open", "waiting, older gateway without payment_issue"):
        waiting_html = render("billing/return.html", by_label[label])
        assert "Confirming your subscription" in text_of(waiting_html), label
        assert "data-billing-poll" in waiting_html and "billing-return.js" in waiting_html, label
        assert 'data-initial-problem="false"' in waiting_html, label
        assert 'http-equiv="refresh"' in waiting_html, label

    complete_html = render("billing/return.html", by_label["complete"])
    assert "You are on Starter" in text_of(complete_html) and "data-billing-poll" not in complete_html

    script = (WEB_DIR / "static" / "js" / "billing-return.js").read_text(encoding="utf-8")
    assert "payment_problem === true && !initialProblem" in script, "the poll reloads when a payment problem appears"


def test_connect_approval_posts_request_and_state():
    context = samples_for("connect/approve.html")["signed in"]
    forms = dict(forms_of(render("connect/approve.html", context)))
    for action in ("/connect/approve", "/connect/deny"):
        assert forms[action]["connect_request_id"] == context["connect_request_id"]
        assert forms[action]["connect_state"] == context["connect_state"]
        assert forms[action]["csrf_token"] == context["csrf_token"]


# --------------------------------------------------------------------------- dates the reader's browser re-states

# A page is written on the server in UTC, but the browser extension prints the reader's own zone,
# so for some hours of the day the two named different days for the same reset. Every date a
# customer reads now carries the machine value in <time datetime>, marked data-local-date (or
# data-local-datetime), and static/js/site.js rewrites the text into the reader's zone. The text
# rendered here stays the UTC wording: it is what a reader without JavaScript sees.
CUSTOMER_DATE_PAGES = [
    ("account/overview.html", "starter"),
    ("account/overview.html", "fair use and cancelling"),
    ("account/overview.html", "pro scheduled downgrade"),
    ("account/overview.html", "pro limit reached, store link"),
    ("account/billing.html", "starter cancelling"),
    ("account/billing.html", "pro scheduled downgrade"),
    ("account/billing.html", "free"),
    ("account/devices.html", "two devices"),
    ("account/security.html", "default"),
    ("billing/return.html", "complete"),
    ("pricing.html", "pro limit reached from extension"),
]


def _times(html: str) -> list:
    return [e for e in check_site.ParsedPage(html).elements if e.tag == "time"]


@pytest.mark.parametrize("template,label", CUSTOMER_DATE_PAGES)
def test_customer_dates_carry_a_machine_value_and_a_utc_fallback(template, label):
    marked = [e for e in _times(render(template, samples_for(template)[label]))
              if "data-local-date" in e.attrs or "data-local-datetime" in e.attrs]
    assert marked, f"{template} [{label}] shows a customer date but marks none of them for the local zone"
    for element in marked:
        stamp = element.attrs.get("datetime", "")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), f"bad datetime {stamp!r}"
        seconds = int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        expected = (templating.format_datetime(seconds) if "data-local-datetime" in element.attrs
                    else templating.format_date(seconds))
        assert element.text.strip() == expected, f"{template} [{label}]: fallback text is not the UTC wording"
        assert not ("data-local-date" in element.attrs and "data-local-datetime" in element.attrs)


def test_every_customer_facing_time_element_is_marked_for_the_local_zone():
    """A <time> a customer reads must be marked, or it silently disagrees with the extension."""
    unmarked = []
    for template, label, context in samples.SAMPLES:
        if template.startswith("admin/"):
            continue  # the owner dashboard says UTC in its own text and stays UTC
        for element in _times(render(template, context)):
            if "data-local-date" not in element.attrs and "data-local-datetime" not in element.attrs:
                unmarked.append(f"{template} [{label}]: {element.text.strip()!r}")
    assert not unmarked, "unmarked <time> on customer pages: " + "; ".join(unmarked)


def test_the_owner_dashboard_keeps_its_stamps_in_utc():
    for template, label, context in samples.SAMPLES:
        if not template.startswith("admin/"):
            continue
        html = render(template, context)
        assert "data-local-date" not in html and "data-local-datetime" not in html, f"{template} [{label}]"


def test_site_js_rewrites_only_the_marked_elements():
    source = (WEB_DIR / "static" / "js" / "site.js").read_text(encoding="utf-8")
    assert 'querySelectorAll("time[data-local-date], time[data-local-datetime]")' in source
    assert "innerHTML" not in source  # the Content-Security-Policy and the checker both forbid it


# --------------------------------------------------------------------------- legal pages and placeholders

@pytest.mark.parametrize("name", samples.LEGAL_PAGES)
def test_legal_banner_and_placeholders_follow_the_environment(name):
    template = f"legal/{name}.html"
    by_label = samples_for(template)
    draft, production = render(template, by_label["draft"]), render(template, by_label["production"])
    assert check_site.DRAFT_BANNER in text_of(draft)
    assert templating.find_placeholders(draft)
    assert '<mark class="placeholder">[[' in draft
    assert check_site.DRAFT_BANNER not in text_of(production)
    assert templating.find_placeholders(production) == []


def test_placeholders_never_appear_outside_legal_pages():
    for template, label, context in samples.SAMPLES:
        if not template.startswith("legal/"):
            assert templating.find_placeholders(render(template, context)) == [], (template, label)


def test_production_flags_legal_values_the_gateway_cannot_set_yet():
    context = samples.base("/privacy", env="production", legal=samples.LEGAL_GATEWAY_FILLED,
                           subprocessors=samples.SUBPROCESSORS_PRODUCTION)
    html = render("legal/privacy.html", context)
    assert templating.find_placeholders(html) == ["[[RETENTION_PERIODS]]"]
    problems = check_site.check_page(html, **check_site.page_options_for_template("legal/privacy.html", context))
    assert any("left in production" in problem for problem in problems)


def test_unset_support_address_sends_people_to_the_contact_page():
    html = render("docs/troubleshooting.html", samples_for("docs/troubleshooting.html")["support address not configured"])
    assert '<a href="/contact">Contact support</a>' in html
    assert "mailto:" not in html


# --------------------------------------------------------------------------- CSS, sources, static files

def test_colour_tokens_meet_wcag_contrast():
    assert check_site.check_tokens((WEB_DIR / "static" / "css" / "tokens.css").read_text(encoding="utf-8")) == []


def test_contrast_maths_and_the_old_faint_colour():
    assert round(check_site.contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert check_site.contrast_ratio("#5d6166", "#000000") < 4.5
    assert check_site.contrast_ratio("#8a8f94", "#000000") >= 4.5
    assert check_site.check_tokens(":root{--bg-page:#000000;--text-faint:#5d6166;--ui-focus:#ffd166}")


def test_template_sources_and_static_files_are_clean():
    assert check_site.check_sources(WEB_DIR) == []
    assert check_site.check_static(WEB_DIR) == []


# --------------------------------------------------------------------------- self-hosted fonts

GOOD_FACE = """/* a comment with url("../fonts/ignored.woff2") in it */
@font-face {
  font-family: "Demo Sans";
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url("../fonts/demo-sans-400.woff2") format("woff2");
}
"""


def _fonts_tree(tmp_path: Path, *, css: str = GOOD_FACE, files: tuple[str, ...] = ("demo-sans-400.woff2",),
                licences: tuple[str, ...] = ("OFL-Demo-Sans.txt",), readme: bool = True) -> Path:
    """A minimal web/ tree with one stylesheet and one font family, for the negative cases."""
    web = tmp_path / "web"
    (web / "static" / "css").mkdir(parents=True)
    (web / "static" / "fonts").mkdir(parents=True)
    (web / "static" / "css" / "tokens.css").write_text(css, encoding="utf-8")
    for name in files:
        (web / "static" / "fonts" / name).write_bytes(b"wOF2 (not a real font file)")
    for name in licences:
        (web / "static" / "fonts" / name).write_text(
            "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n"
            "This license is copied below, and is also available with a FAQ at:\n"
            "http://scripts.sil.org/OFL\n", encoding="utf-8")
    if readme:
        (web / "static" / "fonts" / "README.md").write_text(
            "# Fonts\n\nFetched from the publisher's own service, Latin subset.\n", encoding="utf-8")
    return web


def test_this_site_passes_the_font_rules():
    assert check_site.check_fonts(WEB_DIR) == []


def test_a_correct_font_tree_passes(tmp_path):
    assert check_site.check_fonts(_fonts_tree(tmp_path)) == []


FONT_MUTATIONS = [
    ("no font-display", ("  font-display: swap;\n", ""), "has no font-display: swap"),
    ("font-display optional", ("font-display: swap;", "font-display: optional;"), "has no font-display: swap"),
    ("third-party URL", ('"../fonts/demo-sans-400.woff2"', '"https://fonts.example.com/demo-sans-400.woff2"'),
     "is not a relative URL"),
    ("protocol-relative URL", ('"../fonts/demo-sans-400.woff2"', '"//fonts.example.com/demo-sans-400.woff2"'),
     "is not a relative URL"),
    ("root-relative URL", ('"../fonts/demo-sans-400.woff2"', '"/static/fonts/demo-sans-400.woff2"'),
     "is not a relative URL"),
    ("data URL", ('"../fonts/demo-sans-400.woff2"', '"data:font/woff2;base64,d09GMg=="'), "is not a relative URL"),
    ("file that does not exist", ("demo-sans-400.woff2\"", "demo-sans-700.woff2\""), "does not exist"),
    ("not a woff2", ('"../fonts/demo-sans-400.woff2"', '"../fonts/demo-sans-400.ttf"'), "is not a .woff2 file"),
    ("no src at all", ('  src: url("../fonts/demo-sans-400.woff2") format("woff2");\n', ""), "has no src url()"),
    ("no font-family", ('  font-family: "Demo Sans";\n', ""), "has no font-family"),
]


@pytest.mark.parametrize(("name", "change", "expected"), FONT_MUTATIONS, ids=[m[0] for m in FONT_MUTATIONS])
def test_font_rules_catch_each_mistake(name, change, expected, tmp_path):
    old, new = change
    assert old in GOOD_FACE
    problems = check_site.check_fonts(_fonts_tree(tmp_path, css=GOOD_FACE.replace(old, new, 1)))
    assert any(expected in problem for problem in problems), problems


def test_an_unused_font_file_is_reported(tmp_path):
    web = _fonts_tree(tmp_path, files=("demo-sans-400.woff2", "demo-sans-700.woff2"))
    assert any("demo-sans-700.woff2: not referenced" in p for p in check_site.check_fonts(web))


def test_a_family_without_its_licence_is_reported(tmp_path):
    problems = check_site.check_fonts(_fonts_tree(tmp_path, licences=()))
    assert any("OFL-Demo-Sans.txt is missing" in p for p in problems), problems
    assert check_site.family_licence_name("IBM Plex Sans") == "OFL-IBM-Plex-Sans.txt"
    assert check_site.family_licence_name("Chakra Petch") == "OFL-Chakra-Petch.txt"


def test_fonts_without_a_readme_are_reported(tmp_path):
    assert any("README.md is missing" in p for p in check_site.check_fonts(_fonts_tree(tmp_path, readme=False)))


def test_licence_texts_and_the_readme_are_exempt_from_the_url_rules_only(tmp_path):
    """The OFL text cites the licence's own web address and a repository; both are quotations."""
    assert check_site.is_font_document("static/fonts/OFL-IBM-Plex-Sans.txt")
    assert check_site.is_font_document("static/fonts/README.md")
    assert not check_site.is_font_document("static/fonts/notes.txt")
    assert not check_site.is_font_document("static/css/README.md")
    assert not check_site.is_font_document("static/fonts/sub/OFL-Demo.txt")

    web = _fonts_tree(tmp_path)
    fonts = web / "static" / "fonts"
    (fonts / "OFL-Demo-Sans.txt").write_text(
        "Copyright 2018 The Demo Project Authors (https://github.com/demo/demo-sans.git)\n"
        "Licensed under the SIL Open Font License, Version 1.1: http://scripts.sil.org/OFL\n", encoding="utf-8")
    assert [p for p in check_site.check_static(web) if "OFL-Demo-Sans" in p] == []

    # every other wording rule still applies to a font document
    (fonts / "README.md").write_text("# Fonts\n\nRendered on your own machine.\n", encoding="utf-8")
    assert any("README.md: banned wording" in p for p in check_site.check_static(web))

    # and a plain static text file is still scanned for third-party URLs
    (fonts / "README.md").write_text("# Fonts\n", encoding="utf-8")
    (web / "static" / "notes.txt").write_text("see https://cdn.example.com/x\n", encoding="utf-8")
    assert any("notes.txt: third-party URL" in p for p in check_site.check_static(web))

    # a Markdown file anywhere else is still an unexpected file type
    (web / "static" / "notes.txt").write_text("nothing to see here\n", encoding="utf-8")
    (web / "static" / "css" / "README.md").write_text("# Styles\n", encoding="utf-8")
    assert any("static/css/README.md: unexpected file type" in p for p in check_site.check_static(web))


def test_literal_static_urls_in_a_page_must_exist():
    """base.html preloads the two fonts with literal paths, so static() cannot vouch for them."""
    html = render("index.html", samples_for("index.html")["signed out, gateway context"])
    assert '"/static/fonts/chakra-petch-600.woff2"' in html
    assert '"/static/fonts/ibm-plex-sans-variable.woff2"' in html
    assert check_site.check_static_references(WEB_DIR, html) == []
    for missing in ('<link href="/static/fonts/no-such-font.woff2">',
                    '<img src="/static/img/no-such-image.png">',
                    '<link href="/static/../templates/base.html">'):
        assert check_site.check_static_references(WEB_DIR, missing), missing


def test_every_preloaded_font_url_equals_the_url_in_the_stylesheet():
    """A preload whose URL differs from the @font-face URL downloads the font twice."""
    html = render("index.html", samples_for("index.html")["signed out, gateway context"])
    preloaded = {e.attrs["href"] for e in check_site.ParsedPage(html).elements
                 if e.tag == "link" and "preload" in e.attrs.get("rel", "") and e.attrs.get("as") == "font"}
    assert preloaded, "base.html should preload the two fonts used above the fold"
    css = (WEB_DIR / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    declared = {"/static/fonts/" + Path(url).name for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", css)}
    for url in preloaded:
        assert "?" not in url, f"{url} must not carry a cache key: the @font-face URL has none"
        assert url in declared, f"{url} is preloaded but no @font-face uses it"
        assert (WEB_DIR / url.lstrip("/")).is_file()


# --------------------------------------------------------------------------- command line, negative control

def _run_checker(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(CHECKER), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=300)


def test_cli_templates_mode_passes_on_this_site():
    result = _run_checker("--templates")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("SITE CHECK: PASS")


BAD_TEMPLATE = """<!DOCTYPE html>
<html><head><title>Bad</title><style>body { color: red }</style></head>
<body>
<h1>Bad page</h1><h1>Twice</h1><h3>Skipped a level</h3>
<p style="color:red" onclick="steal()">Trusted by 10,000 users. BRAND money-back guarantee.</p>
<script>alert("inline")</script>
<img src="https://cdn.example.com/tracker.png">
<form method="post" action="/login"><input name="email" type="email"></form>
<p>{{ company }}</p>
</body></html>
"""


def test_negative_control_bad_template_fails_the_checker(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "templates" / "bad.html").write_text(BAD_TEMPLATE.replace("BRAND", "Northwind"), encoding="utf-8")
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"brand": {"retired name": r"(?i)\bnorthwind\b"}}), encoding="utf-8")
    (tmp_path / "tests" / "sample_contexts.py").write_text(
        'SAMPLES = [("bad.html", "negative control", {"site": {"env": "development", '
        '"public_url": "https://starling.example"}, "company": "[[OPERATOR_COMPANY_NAME]]"})]\n', encoding="utf-8")
    result = _run_checker("--templates", "--web-dir", str(tmp_path), "--private-names", str(names))
    assert result.returncode == 1
    for expected in ("inline <script>", "<style> element", "style attribute", "event handler",
                     "exactly one <h1>", "heading level skipped", "skip link", '<main id="main">',
                     "has no label", "no autocomplete", "no alt attribute", "csrf_token", "aria-live",
                     "off-origin resource", "banned wording (private name: retired name)", "banned wording (social proof)",
                     "banned wording (user count)", "banned wording (guarantee)", "outside legal pages",
                     "<html> has no lang", "noindex"):
        assert expected in result.stdout, f"missing {expected!r} in:\n{result.stdout}"


# --------------------------------------------------------------------------- crawl mode against a local server

CONTENT_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".png": "image/png"}


class SampleSite:
    """Serves the templates on 127.0.0.1 the way the gateway would, optionally broken on purpose."""

    def __init__(self, env: str = "development", *, csp: str = GATEWAY_CSP, broken: tuple[str, ...] = ()):
        self.env, self.csp, self.broken = env, csp, set(broken)

    def __enter__(self) -> "SampleSite":
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server naming)
                site.handle(self)

            def log_message(self, *args) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def context(self, path: str, **extra) -> dict:
        production = self.env == "production"
        legal = samples.LEGAL_GATEWAY_FILLED if "gateway-legal" in self.broken else (
            samples.LEGAL_FILLED if production else samples.LEGAL_GATEWAY_DEFAULTS)
        context = samples.base(path, env=self.env, legal=legal, **extra)
        context["site"]["public_url"] = self.origin
        return context

    def page(self, path: str):
        subprocessors = samples.SUBPROCESSORS_PRODUCTION if self.env == "production" else samples.SUBPROCESSORS_DEV
        if path == "/":
            return "index.html", self.context(path, plans=samples.PLANS)
        if path == "/pricing":
            return "pricing.html", self.context(path, plans=samples.PLANS, unit=samples.UNIT, state=None)
        if path.startswith("/docs/") and path[6:] in samples.DOC_SLUGS:
            return f"docs/{path[6:]}.html", self.context(path)
        if path[1:] in samples.LEGAL_PAGES:
            return f"legal/{path[1:]}.html", self.context(path, subprocessors=subprocessors)
        if path == "/login":
            return "auth/login.html", self.context(path, errors={}, form={"email": ""}, next="/account")
        if path == "/signup":
            return "auth/signup.html", self.context(path, errors={}, form={"email": ""})
        if path == "/forgot-password":
            return "auth/forgot.html", self.context(path, sent=False, errors={}, form={"email": ""})
        if path == "/extension/removed":
            return "extension_removed.html", self.context(path)
        return None

    @staticmethod
    def send(handler, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)

    def handle(self, handler) -> None:
        path = urlparse(handler.path).path
        if path.startswith("/static/"):
            root = (WEB_DIR / "static").resolve()
            target = (root / path[len("/static/"):]).resolve()
            if root in target.parents and target.is_file():
                return self.send(handler, 200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
            return self.send(handler, 404, b"not found", "text/plain")
        if path == "/robots.txt":
            block_all = (self.env != "production") != ("robots-wrong" in self.broken)
            rules = "Disallow: /\n" if block_all else "Disallow: /account\nDisallow: /admin\nDisallow: /api/\nDisallow: /write\n"
            return self.send(handler, 200, f"User-agent: *\n{rules}Sitemap: {self.origin}/sitemap.xml\n".encode(), "text/plain")
        if path == "/sitemap.xml":
            paths = list(check_site.PUBLIC_PATHS) + (["/account"] if "sitemap-private" in self.broken else [])
            urls = "".join(f"<url><loc>{self.origin}{p}</loc></url>" for p in paths)
            return self.send(handler, 200, f'<?xml version="1.0"?><urlset>{urls}</urlset>'.encode(), "application/xml")
        if path == "/.well-known/security.txt":
            return self.send(handler, 200, f"Contact: mailto:{samples.SUPPORT_EMAIL}\n".encode(), "text/plain")
        if path == "/manifest.webmanifest":  # web/write.py: the installable app
            return self.send(handler, 200, b'{"name": "Starling", "start_url": "/write"}', "application/manifest+json")
        if path.startswith(("/account", "/admin", "/write")):
            handler.send_response(303)
            handler.send_header("Location", "/login?next=" + quote(path, safe=""))
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return None
        found = self.page(path)
        if found is None:
            return self.send(handler, 404, b"<!doctype html><title>Not found</title>", "text/html; charset=utf-8")
        template, context = found
        html = render(template, context)
        if template == "index.html" and "offsite-image" in self.broken:
            html = html.replace("</main>", '<img src="https://cdn.example.com/pixel.png" alt=""></main>')
        if template == "index.html" and "dead-link" in self.broken:
            html = html.replace("</main>", '<p><a href="/docs/old-guide">Old guide</a></p></main>')
        return self.send(handler, 200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": self.csp})


@pytest.mark.parametrize("env", ["development", "production"])
def test_crawl_passes_on_a_correct_server(env):
    with SampleSite(env) as site:
        problems, requests = check_site.crawl(site.origin, env)
    assert problems == []
    assert requests > len(check_site.PUBLIC_PATHS)


def test_crawl_fails_on_a_broken_server():
    broken = ("offsite-image", "dead-link", "gateway-legal", "robots-wrong", "sitemap-private")
    with SampleSite("production", csp="default-src 'self'; script-src 'self' 'unsafe-inline'; img-src *", broken=broken) as site:
        problems, _ = check_site.crawl(site.origin, "production")
    report = "\n".join(problems)
    for expected in ("allows 'unsafe-inline'", "wildcard", "frame-ancestors is not 'none'", "off-origin resource",
                     "/docs/old-guide: HTTP 404", "placeholder(s) left in production", "blocks the whole site in production",
                     "lists private page /account"):
        assert expected in report, f"missing {expected!r} in:\n{report}"


def test_crawl_requires_robots_to_block_everything_outside_production():
    with SampleSite("development", broken=("robots-wrong",)) as site:
        problems, _ = check_site.crawl(site.origin, "development")
    assert any("must disallow everything" in problem for problem in problems)


# --------------------------------------------------------------------------- contract with the gateway

def _gateway_required_context() -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    if not GATEWAY_RENDER.is_file():
        pytest.skip("backend/server/starling_server/web/render.py is not present")
    required: dict[str, tuple[str, ...]] = {}
    legal_pages: tuple[str, ...] = ()
    others: tuple[str, ...] = ()
    for node in ast.parse(GATEWAY_RENDER.read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        name = getattr(target, "id", None)
        if name == "REQUIRED_CONTEXT":
            required = ast.literal_eval(node.value)
        elif name == "LEGAL_PAGES":
            legal_pages = ast.literal_eval(node.value)
        elif name == "OTHER_TEMPLATES":
            others = ast.literal_eval(node.value)
    required.update({f"legal/{page}.html": ("subprocessors",) for page in legal_pages})
    return required, others


def test_templates_render_with_only_the_keys_the_gateway_sends():
    required, others = _gateway_required_context()
    names = set(check_site.template_names(WEB_DIR))
    assert (set(required) | set(others)) - names == set()
    for template, keys in required.items():
        contexts = samples_for(template)
        assert contexts, f"no sample for {template}"
        for label, context in contexts.items():
            assert set(keys) <= set(context), (template, label, set(keys) - set(context))
            minimal = {key: copy.deepcopy(context[key]) for key in ("site", "user", "csrf_token", "flash", "request_path", *keys)}
            minimal["site"].pop("extension_install_url", None)
            render(template, minimal)


def test_a_plain_jinja_environment_cannot_render_these_templates():
    """Documents why the gateway must pass env=create_environment(...) to Jinja2Templates."""
    starlette_templating = pytest.importorskip("starlette.templating")
    ready = starlette_templating.Jinja2Templates(env=templating.create_environment(WEB_DIR))
    context = samples_for("index.html")["signed out, gateway context"]
    assert "Your voice. Your drafts." in ready.get_template("index.html").render(**context)
    plain = starlette_templating.Jinja2Templates(directory=str(WEB_DIR / "templates"))
    with pytest.raises((jinja2.TemplateAssertionError, jinja2.UndefinedError)):
        plain.get_template("index.html").render(**context)
