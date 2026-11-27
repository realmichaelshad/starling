"""The HTML version of every transactional email, made from its plain text.

Each message still goes out with its plain text first (`text/plain`, then `text/html` as the
alternative), so nothing depends on this file: a client that shows only text shows exactly what it
showed before. The HTML version is the same words in Starling's colours: a black band with the
name, the paragraphs, the message's one action as a button (the first link in the text), any later
links as plain links, and the sign-up code large enough to read at a glance.

Everything is escaped; the only markup is built here. Styles are inline because email clients drop
style sheets, and the layout is one table because many still ignore anything newer.
"""
from __future__ import annotations

import re
from html import escape

# The button on each message, named for what it does. A template not listed gets a plain link only.
BUTTON_LABELS = {
    "verify_email": "Confirm my email",
    "password_reset": "Choose a new password",
    "password_reset_google": "Go to sign in",
    "email_change_confirm": "Confirm the new address",
    "email_change_notice": "Secure my account",
    "signup_existing": "Sign in",
    "email_in_use_notice": "Sign in",
    "google_linked_notice": "Open Security",
    "password_reset_notice": "Reset my password again",
    "password_changed_notice": "Reset my password",
    "password_added_notice": "Reset my password",
    "google_unlinked_notice": "Reset my password",
}

_URL_LINE = re.compile(r"^https?://\S+$")
_CODE = re.compile(r"^Your confirmation code is (\d{3} \d{3})$")

INK = "#111111"
MUTED = "#5b5f66"
CREAM = "#faf8f3"
PAGE = "#f1eee6"


def _paragraph(text: str) -> str:
    lines = [escape(line) for line in text.split("\n")]
    return (f'<p style="margin:0 0 16px;font:16px/1.6 Arial,Helvetica,sans-serif;color:{INK};">'
            + "<br>".join(lines) + "</p>")


def _button(url: str, label: str) -> str:
    href = escape(url, quote=True)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 12px;"><tr>'
        f'<td style="background:{INK};border-radius:8px;">'
        f'<a href="{href}" style="display:inline-block;padding:13px 22px;font:bold 15px/1 Arial,Helvetica,sans-serif;'
        f'color:{CREAM};text-decoration:none;">{escape(label)}</a></td></tr></table>'
        f'<p style="margin:0 0 20px;font:12px/1.5 Arial,Helvetica,sans-serif;color:{MUTED};word-break:break-all;">'
        f'Or copy this link into your browser: <a href="{href}" style="color:{MUTED};">{escape(url)}</a></p>'
    )


def _link(url: str) -> str:
    href = escape(url, quote=True)
    return (f'<p style="margin:0 0 16px;font:15px/1.5 Arial,Helvetica,sans-serif;word-break:break-all;">'
            f'<a href="{href}" style="color:{INK};">{escape(url)}</a></p>')


def _code(code: str) -> str:
    return (f'<p style="margin:0 0 6px;font:14px/1.4 Arial,Helvetica,sans-serif;color:{MUTED};">Your confirmation code</p>'
            f'<p style="margin:0 0 20px;font:bold 32px/1.2 Consolas,Menlo,monospace;letter-spacing:6px;color:{INK};">'
            f"{escape(code)}</p>")


def render(template: str, subject: str, body: str) -> str:
    """The HTML alternative of one message (`body` is the plain text, footer included)."""
    text, _, footer = body.partition("\n\n-- \n")
    blocks: list[str] = []
    label = BUTTON_LABELS.get(template)
    button_used = False
    for paragraph in (part.strip("\n") for part in text.split("\n\n")):
        if not paragraph:
            continue
        code = _CODE.match(paragraph)
        if code:
            blocks.append(_code(code.group(1)))
        elif _URL_LINE.match(paragraph):
            if label and not button_used:
                blocks.append(_button(paragraph, label))
                button_used = True
            else:
                blocks.append(_link(paragraph))
        else:
            blocks.append(_paragraph(paragraph))
    footer_lines = [escape(line) for line in footer.strip().split("\n") if line.strip()]
    footer_html = "<br>".join(footer_lines)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(subject)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAGE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE};"><tr>'
        '<td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">'
        f'<tr><td style="background:{INK};border-radius:12px 12px 0 0;padding:18px 28px;">'
        f'<span style="font:bold 15px/1 Arial,Helvetica,sans-serif;letter-spacing:4px;color:{CREAM};">STARLING</span>'
        "</td></tr>"
        '<tr><td style="background:#ffffff;border-radius:0 0 12px 12px;padding:28px 28px 12px;">'
        + "".join(blocks)
        + "</td></tr>"
        f'<tr><td style="padding:16px 28px;font:12px/1.6 Arial,Helvetica,sans-serif;color:{MUTED};">{footer_html}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
