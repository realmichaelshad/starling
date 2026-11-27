"""`form-action` and the redirect to the payment pages.

Found by the first browser journey that tried to pay: the billing form posts to this site and is
answered with a redirect to a hosted payment page, and a browser applies the page's `form-action`
policy to that redirect. An origin missing from the policy means "Choose Starter" silently does
nothing, and no HTTP-level test can notice, because only a browser enforces the policy.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.config import load_settings, validate
from starling_server.security.middleware import CONTENT_SECURITY_POLICY, content_security_policy

from .conftest import PUBLIC_URL, base_env


def form_action(policy: str) -> list[str]:
    [directive] = [part.strip() for part in policy.split(";") if part.strip().startswith("form-action")]
    return directive.split()[1:]


def test_the_policy_names_every_stripe_hosted_origin_the_site_redirects_to():
    """Checkout, the Customer Portal and a hosted invoice live on three Stripe hosts; all are redirect targets."""
    assert form_action(CONTENT_SECURITY_POLICY) == [
        "'self'", "https://checkout.stripe.com", "https://billing.stripe.com", "https://invoice.stripe.com",
    ]


def test_every_page_of_the_running_site_carries_it(client):
    for path in ("/", "/pricing", "/login", "/signup"):
        policy = client.get(path).headers["content-security-policy"]
        assert "https://checkout.stripe.com" in form_action(policy), path
        assert "https://billing.stripe.com" in form_action(policy), path
        # A declined first payment is finished on the invoice's own page (`CheckoutResult.kind == "invoice"`).
        assert "https://invoice.stripe.com" in form_action(policy), path


def test_the_harness_may_name_its_stand_in_payment_pages(tmp_path):
    env = dict(base_env(tmp_path / "data"), STARLING_E2E_PAYMENT_PAGES_ORIGIN="https://127.0.0.1:55275/")
    settings = load_settings(env)
    assert form_action(content_security_policy(settings))[-1] == "https://127.0.0.1:55275"
    with TestClient(create_app(settings), base_url=PUBLIC_URL) as site:
        assert "https://127.0.0.1:55275" in form_action(site.get("/pricing").headers["content-security-policy"])


def test_only_a_loopback_https_origin_is_ever_added(tmp_path):
    for value in ("http://127.0.0.1:55275", "https://evil.example", "https://127.0.0.1.evil.example",
                  "https://127.0.0.1:55275/path", "*", "https://127.0.0.1:55275 https://evil.example"):
        settings = load_settings(dict(base_env(tmp_path / "data"), STARLING_E2E_PAYMENT_PAGES_ORIGIN=value))
        assert form_action(content_security_policy(settings)) == form_action(CONTENT_SECURITY_POLICY), value


def test_production_refuses_the_setting_and_never_widens_the_policy(tmp_path):
    env = dict(base_env(tmp_path / "data"), STARLING_ENV="production", STARLING_ISOLATION="os",
               STARLING_E2E_PAYMENT_PAGES_ORIGIN="https://127.0.0.1:55275")
    settings = load_settings(env)
    assert any("STARLING_E2E_PAYMENT_PAGES_ORIGIN" in problem for problem in validate(settings, "gateway"))
    assert form_action(content_security_policy(settings)) == form_action(CONTENT_SECURITY_POLICY)
