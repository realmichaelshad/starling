"""Billing failures must explain the failed action on the redirected page."""
import pytest

from starling_server.app import website_templates
from starling_server.billing import service
from starling_server.billing.stripe_client import StripeError
from starling_server.config import REPO_ROOT

from .conftest import make_account
from .test_routes import web, sign_in, post  # reuse real session/CSRF fixture


@pytest.mark.parametrize("path,fields,message", [
    ("/billing/checkout", {"plan_code": "missing"}, "Choose one of the listed plans."),
    ("/billing/portal", {"flow": "unsupported"}, "Unknown billing action."),
])
def test_billing_error_is_visible_after_redirect_and_only_once(web, path, fields, message):
    web.app.state.templates = website_templates(REPO_ROOT / "web")
    token = sign_in(web, make_account(web.db))
    response = post(web, path, token, **fields)
    assert response.status_code == 303
    page = web.http.get(response.headers["location"])
    assert page.status_code == 200
    assert message in page.text
    assert message not in web.http.get(response.headers["location"]).text


def test_provider_error_is_visible_without_leaking_provider_details(web, monkeypatch, caplog):
    web.app.state.templates = website_templates(REPO_ROOT / "web")
    token = sign_in(web, make_account(web.db))
    private_detail = "private-provider-error-sentinel"

    def unavailable(*args, **kwargs):
        raise StripeError(private_detail, http_status=503)

    monkeypatch.setattr(service, "create_checkout", unavailable)
    response = post(web, "/billing/checkout", token, plan_code="starter")
    assert response.status_code == 303
    page = web.http.get(response.headers["location"])
    assert page.status_code == 200
    assert "Billing is not available right now. Try again shortly." in page.text
    assert private_detail not in page.text
    assert private_detail not in caplog.text
