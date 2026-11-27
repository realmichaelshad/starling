"""Every email carries an HTML version in Starling's colours, with its plain text first."""
from __future__ import annotations

from starling_server.email import sender
from starling_server.email import templates as mail


def test_a_message_is_plain_text_first_with_an_html_alternative(settings):
    content = mail.verify_email(settings, "https://starling.example/verify-email?token=abc", "482915")
    message = sender.build_message(settings, "ana@example.com", content.subject, content.body, content.template)
    assert message.get_content_type() == "multipart/alternative"
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert plain.startswith("Welcome to Starling.")
    assert "Confirm my email" in html and "482 915" in html
    assert 'href="https://starling.example/verify-email?token=abc"' in html


def test_whatever_a_message_contains_is_escaped_in_the_html(settings):
    message = sender.build_message(settings, "ana@example.com", "Hi", "<script>alert(1)</script>\n\nhttps://x.example/?a=1&b=2",
                                   "signup_existing")
    html = message.get_body(("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="https://x.example/?a=1&amp;b=2"' in html


def test_replies_reach_support_once_there_is_a_real_address(settings):
    from dataclasses import replace

    with_support = replace(settings, support_email="help@starling.example")
    message = sender.build_message(with_support, "ana@example.com", "Hi", "Body", "verify_email")
    assert message["Reply-To"] == "help@starling.example"
    placeholder = replace(settings, support_email="[[OPERATOR_CONTACT_EMAIL]]")
    assert sender.build_message(placeholder, "ana@example.com", "Hi", "Body")["Reply-To"] is None
