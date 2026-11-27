#!/usr/bin/env python3
"""Starling website gate: rendered templates, static assets and a running server.

    python scripts/check_site.py --templates [--web-dir web]
    python scripts/check_site.py --base-url http://127.0.0.1:8080 [--env production]

--templates renders every template in web/templates with the contexts in
web/tests/sample_contexts.py (StrictUndefined, so a missing key fails) and checks each page:
  * no inline scripts (application/ld+json data blocks excepted), <style> elements,
    style= attributes or on*= handlers
  * exactly one <h1>, one <main id="main">, the skip link to #main as the first focusable
    element, no skipped heading levels, unique ids, a lang attribute and a title
  * a label for every form control, alt on every image, autocomplete on text-like inputs
  * a hidden csrf_token in every POST form (never in GET forms), an aria-live error summary in
    every POST form with editable fields, aria-invalid exactly on the fields the summary lists
  * no off-origin resources, no javascript: URLs, off-origin links only to allowed hosts
  * no banned wording (claims left over from the retired desktop product, testimonials, ratings,
    user counts, guarantees, "number one" claims, fake urgency, and every name in the private
    names file, if there is one)
  * [[PLACEHOLDER]] markers only on legal pages and wrapped in <mark class="placeholder">, the draft
    legal banner outside production, no placeholders or banner in production
  * noindex outside production and on private pages
It also checks template sources, static files and the WCAG contrast of the colour tokens.

--base-url crawls a running server without signing in, starting from the public pages. Every
internal link, fragment and asset must resolve; every HTML response needs a CSP without
unsafe-inline, unsafe-eval or wildcards and with frame-ancestors 'none'; pages get the same page
checks; the sitemap must list the public pages and no private ones; robots.txt must block
everything outside production and only private areas in production. Nothing is requested from
any host other than --base-url.

Exit status: 1 when anything fails, 0 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

REPO = Path(__file__).resolve().parents[1]
DEFAULT_WEB_DIR = REPO / "web"

DOC_SLUGS = ("getting-started", "writing", "panel", "plans-and-usage", "privacy-and-data", "troubleshooting")
LEGAL_PATHS = ("/privacy", "/terms", "/refunds", "/subprocessors", "/acceptable-use", "/contact")
PUBLIC_PATHS = ("/", "/pricing", *(f"/docs/{slug}" for slug in DOC_SLUGS), *LEGAL_PATHS)
SITEMAP_FORBIDDEN_PREFIXES = ("/account", "/admin", "/api/", "/billing", "/connect", "/webhooks",
                              "/verify-email", "/reset-password", "/logout", "/write", "/offline")
ROBOTS_PRODUCTION_DISALLOW = ("/account", "/admin", "/api/", "/write")
# "write": the signed-in writing page for phones (web/write.py).
PRIVATE_TEMPLATE_DIRS = ("account", "admin", "auth", "connect", "billing", "errors", "write")
# Pages outside those folders that are never indexed: the extension's goodbye page, and the offline
# page the installed app's service worker keeps.
PRIVATE_TEMPLATES = ("extension_removed.html", "offline.html")
ALLOWED_LINK_HOSTS = (
    "chromewebstore.google.com",
    # "Open Gmail" and friends on the check-your-email page (auth/routes.py MAIL_LINKS).
    "mail.google.com", "outlook.live.com", "mail.yahoo.com", "www.icloud.com", "mail.proton.me",
    # "Open x.com" in the account page's Get started checklist.
    "x.com",
)
SOURCE_URL_HOSTS = (
    "schema.org",  # JSON-LD vocabulary identifier, never fetched
    "x.com",  # "Open x.com" on the account page: the bare front page (a path would trip the profile-link ban)
)
DRAFT_BANNER = "Draft legal text: the operator must complete it and have counsel review it"
PLACEHOLDER_RE = re.compile(r"\[\[[A-Z0-9_]+\]\]")

# Wording the hosted product's pages must never carry. Most of it was true of the desktop product
# Starling used to be and is false now. (The letter in brackets keeps this file from matching a
# private names file that lists the same word.)
#
# Names that must stay off the pages (a retired brand, an internal code name, a person) are private
# to whoever runs the site, so they are not written here: `load_private_names` adds them from
# `scripts/release_private_patterns.json`, the untracked file the release check reads too.
RAW_PATTERNS = [
    ("local model runtime", re.compile(r"(?i)\boll[a]ma\b")),
    ("local-product claim", re.compile(r"(?i)nothing\s+leaves")),
    ("local-product claim", re.compile(r"(?i)\blocal[\s-]+first\b")),
    ("local-product claim", re.compile(r"(?i)\bruns?\s+locally\b")),
    ("local-product claim", re.compile(r"(?i)\byour\s+(?:own\s+)?machine\b")),
    ("desktop OS claim", re.compile(r"(?i)\bwindows\s+1[01]\b")),
    ("installer name", re.compile(r"(?i)starling\s*setup")),
    ("download claim", re.compile(r"(?i)\bfree\s+download\b")),
    ("unpacked install", re.compile(r"(?i)\bload\s+unpacked\b")),
    ("local companion address", re.compile(r"127\.0\.0\.1:8787|localhost:8787")),
    ("local data folder", re.compile(r"(?i)%local\s*app\s*data%|\blocalappdata\b")),
    ("social profile link", re.compile(
        r"(?i)(?:https?://)?(?:www\.)?(?:x|twitter|github|linkedin|instagram|facebook|youtube)\.com/[A-Za-z0-9_]")),
]
TEXT_PATTERNS = [
    ("testimonial", re.compile(r"(?i)\btestimonials?\b")),
    ("rating markup", re.compile(r"(?i)aggregate\s*rating|\bratingvalue\b|\breviewcount\b|\bstar\s+rating\b")),
    ("social proof", re.compile(r"(?i)\btrusted\s+by\b|\bloved\s+by\b|\bas\s+seen\s+on\b")),
    ("user count", re.compile(
        r"(?i)\b\d[\d,.]*\s*(?:k|m)?\+?\s*(?:happy\s+|active\s+)?(?:users|customers|creators|writers|people|members)\b")),
    ("guarantee", re.compile(r"(?i)\bguarantee")),
    ("number one claim", re.compile(r"(?<![\w&])#\s?1\b|(?i:\bnumber\s+one\b)")),
    ("fake urgency", re.compile(
        r"(?i)\blimited[-\s]time\b|\bact\s+now\b|\bhurry\b|\bonly\s+\d+\s+(?:left|spots|seats)\b|\boffer\s+ends\b")),
]

PRIVATE_NAMES_FILE = REPO / "scripts" / "release_private_patterns.json"


def load_private_names(source: str | None = None) -> int:
    """Add the names in the private names file to the banned wording. Returns how many were added.

    The file is `{"group": {"label": "regular expression", ...}, ...}` (scripts/check_release.py
    describes it). Every group counts here: a name that must not be in the repository must not be
    on a page either. No file, no names: the other checks still run.
    """
    path = Path(source) if source else PRIVATE_NAMES_FILE
    if not path.is_file():
        if source:
            raise SystemExit(f"private names file not found: {path}")
        return 0
    added = 0
    for group in json.loads(path.read_text(encoding="utf-8")).values():
        if not isinstance(group, dict):
            continue
        for label, pattern in group.items():
            RAW_PATTERNS.append((f"private name: {label}", re.compile(str(pattern))))
            added += 1
    return added


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
TEXTLIKE_TYPES = {"text", "email", "password", "number", "search", "tel", "url"}
EDITABLE_TYPES = TEXTLIKE_TYPES | {"checkbox", "radio", "file", "date", "datetime-local", "month", "time", "week", "range", "color"}
RESOURCE_ATTRS = {("img", "src"), ("img", "srcset"), ("script", "src"), ("iframe", "src"), ("source", "src"),
                  ("source", "srcset"), ("audio", "src"), ("video", "src"), ("video", "poster"), ("embed", "src"),
                  ("object", "data"), ("input", "src"), ("track", "src")}
NAVIGATION_LINK_RELS = {"canonical", "alternate", "author", "license", "help", "next", "prev", "search"}


# --------------------------------------------------------------------------- parsing

class Element:
    __slots__ = ("tag", "attrs", "index", "ancestors", "text")

    def __init__(self, tag: str, attrs: dict[str, str], index: int, ancestors: tuple[int, ...]):
        self.tag, self.attrs, self.index, self.ancestors, self.text = tag, attrs, index, ancestors, ""


class ParsedPage(HTMLParser):
    """Flat element list with ancestors and the text inside each element (pages are small)."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.scripts: list[list] = []
        self.text_chunks: list[str] = []
        self.has_doctype = False
        self._stack: list[tuple[str, int]] = []
        self.feed(html)
        self.close()

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.has_doctype = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k.lower(): (v if v is not None else "") for k, v in attrs},
                          len(self.elements), tuple(index for _, index in self._stack))
        self.elements.append(element)
        if tag == "script":
            self.scripts.append([element, ""])
        if tag not in VOID_TAGS:
            self._stack.append((tag, element.index))

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self._stack) - 1, -1, -1):
            if self._stack[position][0] == tag:
                del self._stack[position:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][0] == "script":
            self.scripts[-1][1] += data
            return
        if self._stack and self._stack[-1][0] == "style":
            return
        self.text_chunks.append(data)
        for _, index in self._stack:
            self.elements[index].text += data

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_chunks)).strip()

    def hidden(self, element: Element) -> bool:
        chain = [element, *(self.elements[i] for i in element.ancestors)]
        return any("hidden" in e.attrs or e.attrs.get("aria-hidden") == "true" for e in chain)

    def inside(self, container: Element) -> list[Element]:
        return [e for e in self.elements if container.index in e.ancestors]


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _same_origin(url: str, origin: str) -> bool:
    if url.startswith("//"):
        return _origin_of(urlparse(origin).scheme + ":" + url) == origin.lower()
    parsed = urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return True
    return parsed.scheme in ("http", "https") and _origin_of(url) == origin.lower()


def _focusable(page: ParsedPage, element: Element) -> bool:
    if page.hidden(element) or "disabled" in element.attrs:
        return False
    tabindex = element.attrs.get("tabindex")
    if tabindex is not None:
        try:
            return int(tabindex) >= 0
        except ValueError:
            return False
    if element.tag == "a":
        return "href" in element.attrs
    if element.tag == "input":
        return element.attrs.get("type", "text").lower() != "hidden"
    return element.tag in {"button", "select", "textarea", "summary", "iframe"}


def _accessible_name(page: ParsedPage, element: Element) -> str:
    if element.attrs.get("aria-label", "").strip():
        return element.attrs["aria-label"].strip()
    alts = " ".join(e.attrs.get("alt", "") for e in page.inside(element) if e.tag == "img")
    return (element.text + " " + alts).strip()


# --------------------------------------------------------------------------- page checks

def check_page(html: str, *, kind: str, env: str, origin: str,
               allowed_link_hosts: tuple[str, ...] = ALLOWED_LINK_HOSTS) -> list[str]:
    """Check one rendered page. kind is "public", "legal" or "private"."""
    page = ParsedPage(html)
    els = page.elements
    problems: list[str] = []
    origin = _origin_of(origin)

    ids: dict[str, list[Element]] = {}
    for e in els:
        if "id" in e.attrs:
            ids.setdefault(e.attrs["id"], []).append(e)
    duplicates = sorted(name for name, found in ids.items() if len(found) > 1)
    if duplicates:
        problems.append(f"duplicate id(s): {', '.join(duplicates)}")
    for e in els:
        for attr in ("aria-labelledby", "aria-describedby", "aria-controls"):
            for ref in e.attrs.get(attr, "").split():
                if ref not in ids:
                    problems.append(f"<{e.tag}> {attr} points at missing id {ref!r}")

    if not page.has_doctype:
        problems.append("missing <!DOCTYPE html>")
    html_tags = [e for e in els if e.tag == "html"]
    if not html_tags or not html_tags[0].attrs.get("lang"):
        problems.append("<html> has no lang attribute")
    titles = [e for e in els if e.tag == "title"]
    if len(titles) != 1 or not titles[0].text.strip():
        problems.append("page needs exactly one non-empty <title>")

    # scripts, styles, handlers
    for element, body in page.scripts:
        if "src" in element.attrs:
            continue
        if element.attrs.get("type", "").strip().lower() != "application/ld+json":
            problems.append("inline <script> (only external scripts and application/ld+json data blocks are allowed)")
            continue
        try:
            json.loads(body)
        except ValueError:
            problems.append("application/ld+json block is not valid JSON")
    if any(e.tag == "style" for e in els):
        problems.append("<style> element (styles belong in static/css)")
    for e in els:
        for name in e.attrs:
            if name == "style":
                problems.append(f"style attribute on <{e.tag}>")
            elif name.startswith("on"):
                problems.append(f"event handler attribute {name} on <{e.tag}>")

    # landmarks, skip link, headings
    h1s = [e for e in els if e.tag == "h1"]
    if len(h1s) != 1:
        problems.append(f"expected exactly one <h1>, found {len(h1s)}")
    mains = [e for e in els if e.tag == "main"]
    if len(mains) != 1 or mains[0].attrs.get("id") != "main":
        problems.append('expected exactly one <main id="main">')
    focusable = [e for e in els if _focusable(page, e)]
    if not focusable or focusable[0].tag != "a" or focusable[0].attrs.get("href") != "#main":
        problems.append('the first focusable element must be the skip link <a href="#main">')
    headings = [e for e in els if re.fullmatch(r"h[1-6]", e.tag)]
    levels = [int(e.tag[1]) for e in headings]
    if levels and levels[0] != 1:
        problems.append(f"first heading is <h{levels[0]}>, expected <h1>")
    for previous, current in zip(levels, levels[1:]):
        if current > previous + 1:
            problems.append(f"heading level skipped: <h{previous}> followed by <h{current}>")
    for e in headings:
        if not e.text.strip():
            problems.append(f"empty <{e.tag}>")

    # controls, labels, images
    labelled_ids = {e.attrs.get("for") for e in els if e.tag == "label" and e.text.strip()}
    for e in els:
        if e.tag in ("input", "select", "textarea"):
            kind_of_input = e.attrs.get("type", "text").lower() if e.tag == "input" else e.tag
            if e.tag == "input" and kind_of_input in ("hidden", "submit", "button", "reset", "image"):
                continue
            name = e.attrs.get("name", "?")
            has_label = (e.attrs.get("id") in labelled_ids
                         or any(els[i].tag == "label" for i in e.ancestors)
                         or e.attrs.get("aria-label", "").strip()
                         or e.attrs.get("aria-labelledby", "").strip())
            if not has_label:
                problems.append(f"<{e.tag} name={name!r}> has no label")
            if (e.tag == "textarea" or kind_of_input in TEXTLIKE_TYPES) and not e.attrs.get("autocomplete", "").strip():
                problems.append(f"<{e.tag} name={name!r}> has no autocomplete attribute")
        elif e.tag == "button" and not _accessible_name(page, e):
            problems.append("<button> without an accessible name")
        elif e.tag == "a" and "href" in e.attrs and not _accessible_name(page, e):
            problems.append(f"link to {e.attrs['href']!r} without an accessible name")
        elif e.tag == "img" and "alt" not in e.attrs:
            problems.append(f"<img src={e.attrs.get('src')!r}> has no alt attribute")
        elif e.tag == "svg" and not any(els[i].tag == "svg" for i in e.ancestors):
            if e.attrs.get("aria-hidden") != "true" and not (e.attrs.get("role") == "img" and e.attrs.get("aria-label")):
                problems.append("inline <svg> must be aria-hidden or have role=img and aria-label")

    # forms
    for form in (e for e in els if e.tag == "form"):
        inside = page.inside(form)
        method = form.attrs.get("method", "get").lower()
        action = form.attrs.get("action", "")
        where = f"form action={action!r}"
        csrf = [e for e in inside if e.tag == "input" and e.attrs.get("name") == "csrf_token"]
        if method == "post":
            if len(csrf) != 1 or csrf[0].attrs.get("type") != "hidden" or not csrf[0].attrs.get("value"):
                problems.append(f"{where}: POST form needs one hidden csrf_token with a value")
            editable = [e for e in inside if e.tag in ("select", "textarea")
                        or (e.tag == "input" and e.attrs.get("type", "text").lower() in EDITABLE_TYPES)]
            if editable and not any("aria-live" in e.attrs for e in inside):
                problems.append(f"{where}: form has fields but no aria-live error summary")
        elif csrf:
            problems.append(f"{where}: GET form must not carry csrf_token (it would leak into URLs)")
        if action and not _same_origin(action, origin):
            problems.append(f"{where}: action is not same-origin")

    linked_fields: set[str] = set()
    for summary in (e for e in els if "data-error-summary" in e.attrs):
        inside = page.inside(summary)
        items = [e for e in inside if e.tag == "li"]
        if "aria-live" not in summary.attrs:
            problems.append("error summary without aria-live")
        if items and "hidden" in summary.attrs:
            problems.append("error summary has messages but is hidden")
        if not items and "hidden" not in summary.attrs:
            problems.append("empty error summary is not hidden")
        for link in (e for e in inside if e.tag == "a" and e.attrs.get("href", "").startswith("#")):
            target = link.attrs["href"][1:]
            linked_fields.add(target)
            if target not in ids:
                problems.append(f"error summary links to missing field #{target}")
            elif ids[target][0].attrs.get("aria-invalid") != "true":
                problems.append(f'#{target} is listed in an error summary but lacks aria-invalid="true"')
    for e in els:
        if e.attrs.get("aria-invalid") == "true" and e.attrs.get("id") not in linked_fields:
            problems.append(f"#{e.attrs.get('id')} has aria-invalid=\"true\" but no error summary links to it")

    # URLs
    for e in els:
        for attr in ("href", "src", "srcset", "poster", "data", "action", "formaction"):
            if attr not in e.attrs:
                continue
            raw = e.attrs[attr].strip()
            urls = [part.strip().split()[0] for part in raw.split(",") if part.strip()] if attr == "srcset" else [raw]
            for url in urls:
                if url.lower().startswith("javascript:"):
                    problems.append(f"javascript: URL on <{e.tag}>")
                    continue
                if url.startswith("#"):
                    if len(url) > 1 and url[1:] not in ids:
                        problems.append(f"link to missing fragment {url}")
                    continue
                if url.lower().startswith("mailto:"):
                    continue
                resource = (e.tag, attr) in RESOURCE_ATTRS or (
                    e.tag == "link" and not set(e.attrs.get("rel", "").lower().split()) & NAVIGATION_LINK_RELS)
                if resource:
                    if url.lower().startswith("data:") and e.tag == "img":
                        continue
                    if not _same_origin(url, origin):
                        problems.append(f"off-origin resource on <{e.tag} {attr}>: {url}")
                elif not _same_origin(url, origin):
                    parsed = urlparse(url)
                    if e.tag == "a" and parsed.scheme == "https" and (parsed.hostname or "") in allowed_link_hosts:
                        continue
                    problems.append(f"off-origin link on <{e.tag} {attr}>: {url}")
        if e.tag == "meta":
            content = e.attrs.get("content", "")
            if e.attrs.get("http-equiv", "").lower() == "refresh":
                target = re.search(r"url\s*=\s*(\S+)", content, re.I)
                if target and not _same_origin(target.group(1).strip("'\""), origin):
                    problems.append("meta refresh points off-origin")
            elif re.match(r"(?i)https?://", content) and not _same_origin(content, origin):
                problems.append(f"off-origin URL in <meta> content: {content}")

    # wording
    attribute_text = " ".join(v for e in els for k, v in e.attrs.items()
                              if k in ("alt", "title", "content", "aria-label", "placeholder"))
    json_text = " ".join(body for element, body in page.scripts if "src" not in element.attrs)
    corpus = " ".join((page.text(), attribute_text, json_text))
    for label, pattern in RAW_PATTERNS:
        match = pattern.search(html)
        if match:
            problems.append(f"banned wording ({label}): {match.group(0)!r}")
    for label, pattern in TEXT_PATTERNS:
        match = pattern.search(corpus)
        if match:
            problems.append(f"banned wording ({label}): {match.group(0)!r}")

    # placeholders and the draft legal banner
    found = PLACEHOLDER_RE.findall(html)
    if found:
        names = ", ".join(sorted(set(found)))
        if kind != "legal":
            problems.append(f"placeholder(s) outside legal pages: {names}")
        elif env == "production":
            problems.append(f"placeholder(s) left in production: {names}")
        else:
            marked = sum(len(PLACEHOLDER_RE.findall(m.text)) for m in els
                         if m.tag == "mark" and "placeholder" in m.attrs.get("class", "").split())
            if marked != len(found):
                problems.append('every placeholder must be wrapped in <mark class="placeholder">')
    has_banner = DRAFT_BANNER in page.text()
    if kind == "legal" and env != "production" and not has_banner:
        problems.append("legal page outside production is missing the draft legal text banner")
    if has_banner and (kind != "legal" or env == "production"):
        problems.append("draft legal text banner shown where it does not belong")

    # indexing
    robots = [e.attrs.get("content", "").lower() for e in els if e.tag == "meta" and e.attrs.get("name", "").lower() == "robots"]
    # A payments rehearsal is production on Stripe's sandbox (STRIPE_ALLOW_TEST_MODE): the page says so
    # in a banner, and like everything that is not the real thing it must not be indexed.
    rehearsal = any("data-payments-test-mode" in e.attrs for e in els)
    if env != "production" or kind == "private" or rehearsal:
        if not robots or "noindex" not in robots[0]:
            problems.append('page must carry <meta name="robots" content="noindex, ...">')
    elif robots and "noindex" in robots[0]:
        problems.append("public page is noindex in production")

    return list(dict.fromkeys(problems))


def template_kind(template: str) -> str:
    if template.startswith("legal/"):
        return "legal"
    if template.split("/")[0] in PRIVATE_TEMPLATE_DIRS or template in PRIVATE_TEMPLATES:
        return "private"
    return "public"


def path_kind(path: str) -> str:
    if path in LEGAL_PATHS:
        return "legal"
    if path in ("/", "/pricing") or path.startswith("/docs/"):
        return "public"
    return "private"


def page_options_for_template(template: str, context: dict) -> dict:
    return {"kind": template_kind(template), "env": context["site"]["env"], "origin": context["site"]["public_url"]}


# --------------------------------------------------------------------------- sources, static, tokens

def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    channels = []
    for i in (0, 2, 4):
        c = int(value[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def check_tokens(css: str) -> list[str]:
    tokens = {name: value for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css)}
    colours = {n: v.strip() for n, v in tokens.items() if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", v.strip())}
    problems = []
    groups = {prefix: {n: v for n, v in tokens.items() if n.startswith(prefix)} for prefix in ("--bg-", "--text-", "--ui-")}
    for prefix, members in groups.items():
        if not members:
            problems.append(f"tokens.css defines no {prefix}* colours")
        for name, value in members.items():
            if name not in colours:
                problems.append(f"{name} must be a 3- or 6-digit hex colour, found {value.strip()!r}")
    backgrounds = {n: colours[n] for n in groups["--bg-"] if n in colours}
    for minimum, prefix in ((4.5, "--text-"), (3.0, "--ui-")):
        for name in groups[prefix]:
            if name not in colours:
                continue
            for bg_name, bg in backgrounds.items():
                ratio = contrast_ratio(colours[name], bg)
                if ratio < minimum:
                    problems.append(f"{name} {colours[name]} on {bg_name} {bg} is {ratio:.2f}:1, needs {minimum}:1")
    for name, value in colours.items():
        if name.endswith("-fg"):
            partner = name[:-3] + "-bg"
            if partner not in colours:
                problems.append(f"{name} has no matching {partner}")
            elif contrast_ratio(value, colours[partner]) < 4.5:
                problems.append(f"{name} on {partner} is {contrast_ratio(value, colours[partner]):.2f}:1, needs 4.5:1")
    return problems


def check_sources(web_dir: Path) -> list[str]:
    problems = []
    for path in sorted((web_dir / "templates").rglob("*.html")):
        rel = path.relative_to(web_dir).as_posix()
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
        if re.search(r"\sstyle\s*=", text):
            problems.append(f"{rel}: style attribute in template source")
        if re.search(r"<\s*style[\s>]", text, re.I):
            problems.append(f"{rel}: <style> element in template source")
        if re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I):
            problems.append(f"{rel}: event handler attribute in template source")
        for match in re.finditer(r"<script\b([^>]*)>", text, re.I):
            if "src=" not in match.group(1) and "application/ld+json" not in match.group(1):
                problems.append(f"{rel}: inline <script> in template source")
        if re.search(r"\|\s*safe\b", text) or re.search(r"autoescape\s+false", text):
            problems.append(f"{rel}: bypasses autoescaping")
        if PLACEHOLDER_RE.search(text):
            problems.append(f"{rel}: literal [[PLACEHOLDER]] in template source (render site.legal values instead)")
        for url in re.findall(r"https?://[^\s\"'<>)]+", text):
            if (urlparse(url).hostname or "") not in SOURCE_URL_HOSTS:
                problems.append(f"{rel}: hard-coded third-party URL {url}")
        for label, pattern in RAW_PATTERNS:
            if pattern.search(text):
                problems.append(f"{rel}: banned wording ({label})")
    return problems


def is_font_document(rel: str) -> bool:
    """The licence texts and the README that must ship next to the self-hosted fonts.

    These are verbatim legal texts (SIL Open Font License 1.1) and the note that records where the
    files came from, so they are exempt from two rules only: the third-party-URL scan (the licence
    cites scripts.sil.org and a copyright line may cite a repository) and the social-profile-link
    pattern that such a citation matches. Every other wording rule still applies to them.
    """
    return bool(re.fullmatch(r"static/fonts/(?:OFL-[A-Za-z0-9-]+\.txt|README\.md)", rel))


def check_static(web_dir: Path) -> list[str]:
    static = web_dir / "static"
    if not static.is_dir():
        return [f"{static} is missing"]
    problems = []
    allowed = {".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff2", ".txt", ".json"}
    for path in sorted(p for p in static.rglob("*") if p.is_file()):
        rel = path.relative_to(web_dir).as_posix()
        font_document = is_font_document(rel)
        if path.suffix.lower() not in allowed and not font_document:
            problems.append(f"{rel}: unexpected file type")
            continue
        if path.suffix.lower() not in {".css", ".js", ".svg", ".txt", ".json"} and not font_document:
            continue
        text = path.read_text(encoding="utf-8")
        if not font_document:
            for url in re.findall(r"(?:https?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", text):
                problems.append(f"{rel}: third-party URL {url}")
        if path.suffix == ".css" and re.search(r"@import|expression\s*\(", text):
            problems.append(f"{rel}: @import or expression() in CSS")
        if path.suffix == ".js":
            for risky in (r"\beval\s*\(", r"new\s+Function\s*\(", r"\.innerHTML\b", r"\.outerHTML\b",
                          r"document\.write", r"insertAdjacentHTML", r"setTimeout\s*\(\s*['\"]"):
                if re.search(risky, text):
                    problems.append(f"{rel}: unsafe DOM or eval pattern {risky}")
        for label, pattern in RAW_PATTERNS:
            if font_document and label == "social profile link":
                continue
            if pattern.search(text):
                problems.append(f"{rel}: banned wording ({label})")
    site_css = static / "css" / "site.css"
    if site_css.is_file():
        css = site_css.read_text(encoding="utf-8")
        for needle in (":focus-visible", "prefers-reduced-motion"):
            if needle not in css:
                problems.append(f"static/css/site.css: no {needle} rule")
    tokens = static / "css" / "tokens.css"
    if tokens.is_file():
        problems += [f"static/css/tokens.css: {p}" for p in check_tokens(tokens.read_text(encoding="utf-8"))]
    else:
        problems.append("static/css/tokens.css is missing")
    problems += check_fonts(web_dir)
    return problems


def family_licence_name(family: str) -> str:
    """"IBM Plex Sans" -> "OFL-IBM-Plex-Sans.txt"."""
    return "OFL-" + "-".join(family.split()) + ".txt"


def check_fonts(web_dir: Path) -> list[str]:
    """Self-hosted fonts: every face swaps, points at a file that exists, and carries its licence.

    Web fonts are the one kind of asset that can hide a third-party request, block the first paint
    or ship without the licence its authors require, so each @font-face is checked for
    `font-display: swap` and a relative ../fonts/<name>.woff2 URL, every shipped .woff2 must be
    used by one of them, and every family must have the OFL text that has to travel with it.
    """
    static = web_dir / "static"
    fonts_dir = static / "fonts"
    css_dir = static / "css"
    problems: list[str] = []
    referenced: set[str] = set()
    families: set[str] = set()
    for css_path in sorted(css_dir.glob("*.css")) if css_dir.is_dir() else []:
        rel = css_path.relative_to(web_dir).as_posix()
        text = re.sub(r"/\*.*?\*/", "", css_path.read_text(encoding="utf-8"), flags=re.S)
        for block in re.findall(r"@font-face\s*\{([^}]*)\}", text, re.S):
            family_match = re.search(r"font-family\s*:\s*[\"']?([^\"';]+?)[\"']?\s*;", block)
            family = family_match.group(1).strip() if family_match else ""
            label = f"@font-face {family or '(no font-family)'}"
            if not family:
                problems.append(f"{rel}: {label} has no font-family")
            else:
                families.add(family)
            if not re.search(r"font-display\s*:\s*swap\s*;", block):
                problems.append(f"{rel}: {label} has no font-display: swap")
            urls = re.findall(r"url\(\s*['\"]?([^'\")]+)", block)
            if not urls:
                problems.append(f"{rel}: {label} has no src url()")
            for url in urls:
                if url.startswith(("http:", "https:", "//", "data:")) or url.startswith("/"):
                    problems.append(f"{rel}: {label} src {url} is not a relative URL")
                    continue
                if not url.lower().endswith(".woff2"):
                    problems.append(f"{rel}: {label} src {url} is not a .woff2 file")
                    continue
                target = (css_path.parent / url).resolve()
                if not target.is_file():
                    problems.append(f"{rel}: {label} src {url} does not exist")
                    continue
                referenced.add(target.name)
    if not fonts_dir.is_dir():
        if families:
            problems.append("static/fonts is missing")
        return problems
    for path in sorted(fonts_dir.glob("*.woff2")):
        if path.name not in referenced:
            problems.append(f"static/fonts/{path.name}: not referenced by any @font-face")
    for family in sorted(families):
        licence = family_licence_name(family)
        if not (fonts_dir / licence).is_file():
            problems.append(f"static/fonts/{licence} is missing (licence for {family})")
    if not (fonts_dir / "README.md").is_file():
        problems.append("static/fonts/README.md is missing (record the source and licence of each font)")
    return problems


def check_static_references(web_dir: Path, html: str) -> list[str]:
    """Every literal /static/... URL written into a page must exist on disk.

    Templates normally use static(), which hashes the file and fails when it is missing; the font
    preloads cannot, because their URL has to equal the URL inside the stylesheet exactly.
    """
    static = (web_dir / "static").resolve()
    problems = []
    for url in sorted(set(re.findall(r"[\"'](/static/[^\"'\s>]+)", html))):
        path = urlparse(url).path
        target = (web_dir / path.lstrip("/")).resolve()
        if static not in target.parents or not target.is_file():
            problems.append(f"{url} does not exist on disk")
    return problems


# --------------------------------------------------------------------------- --templates

def _load_module(path: Path, prefix: str):
    name = f"{prefix}_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def template_names(web_dir: Path) -> list[str]:
    root = web_dir / "templates"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.html")
                  if not p.name.startswith("_") and p.name != "base.html")


def check_templates(web_dir: Path) -> tuple[list[str], int]:
    templating_path = web_dir / "templating.py"
    if not templating_path.is_file():
        templating_path = DEFAULT_WEB_DIR / "templating.py"
    templating = _load_module(templating_path, "starling_site_templating")
    samples = _load_module(web_dir / "tests" / "sample_contexts.py", "starling_site_samples").SAMPLES
    env = templating.create_environment(web_dir, strict=True)
    problems: list[str] = []
    names = set(template_names(web_dir))
    covered = {template for template, _, _ in samples}
    problems += [f"{name}: no sample context" for name in sorted(names - covered)]
    problems += [f"{name}: sample context for a template that does not exist" for name in sorted(covered - names)]
    for template, label, context in samples:
        if template not in names:
            continue
        try:
            html = env.get_template(template).render(**context)
        except Exception as exc:  # report every broken template, not only the first
            problems.append(f"{template} [{label}]: render failed: {type(exc).__name__}: {exc}")
            continue
        problems += [f"{template} [{label}]: {p}" for p in check_page(html, **page_options_for_template(template, context))]
        problems += [f"{template} [{label}]: {p}" for p in check_static_references(web_dir, html)]
    problems += check_sources(web_dir)
    problems += check_static(web_dir)
    return problems, len(samples)


# --------------------------------------------------------------------------- --base-url

def _fetch(opener, url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": "starling-check-site/1", "Accept": "*/*"})
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc), {}, b""
    with response:
        headers: dict[str, list[str]] = {}
        for key, value in response.headers.items():
            headers.setdefault(key.lower(), []).append(value)
        return response.status, response.geturl(), headers, response.read(5_000_000)


def check_csp(values: list[str]) -> list[str]:
    if not values:
        return ["no Content-Security-Policy header"]
    directives: dict[str, list[str]] = {}
    for part in ";".join(values).split(";"):
        tokens = part.split()
        if tokens:
            directives.setdefault(tokens[0].lower(), [t.lower() for t in tokens[1:]])
    problems = []
    for name, sources in directives.items():
        for source in sources:
            if source in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "'wasm-unsafe-eval'"):
                problems.append(f"CSP {name} allows {source}")
            elif "*" in source:
                problems.append(f"CSP {name} uses the wildcard {source}")
            elif source in ("http:", "https:", "ws:", "wss:", "blob:", "filesystem:"):
                problems.append(f"CSP {name} allows any source over {source}")
            elif source == "data:" and name != "img-src":
                problems.append(f"CSP {name} allows data:")
    if directives.get("frame-ancestors") != ["'none'"]:
        problems.append("CSP frame-ancestors is not 'none'")
    if "default-src" not in directives:
        problems.append("CSP has no default-src")
    if directives.get("object-src", directives.get("default-src")) != ["'none'"]:
        problems.append("CSP object-src is not 'none'")
    if directives.get("base-uri") not in (["'none'"], ["'self'"]):
        problems.append("CSP base-uri is not restricted")
    if "script-src" not in directives and directives.get("default-src") != ["'none'"]:
        problems.append("CSP has no script-src")
    return problems


def check_robots(opener, base: str, env: str, timeout: float) -> list[str]:
    status, _, _, body = _fetch(opener, base + "/robots.txt", timeout)
    if status != 200:
        return [f"/robots.txt: HTTP {status}"]
    text = body.decode("utf-8", "replace")
    disallowed: list[str] = []
    applies = False
    for raw in text.splitlines():
        key, _, value = raw.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            applies = value == "*"
        elif key == "disallow" and applies and value:
            disallowed.append(value)
    problems = []
    if env == "production":
        if "/" in disallowed:
            problems.append("/robots.txt blocks the whole site in production")
        for private in ROBOTS_PRODUCTION_DISALLOW:
            if not any(private.startswith(rule) for rule in disallowed):
                problems.append(f"/robots.txt does not disallow {private} in production")
        if not re.search(r"(?im)^\s*sitemap:\s*https?://\S+/sitemap\.xml\s*$", text):
            problems.append("/robots.txt has no Sitemap line in production")
    elif "/" not in disallowed:
        problems.append(f"/robots.txt must disallow everything when the environment is {env}")
    return problems


def check_sitemap(opener, base: str, timeout: float) -> list[str]:
    status, _, _, body = _fetch(opener, base + "/sitemap.xml", timeout)
    if status != 200:
        return [f"/sitemap.xml: HTTP {status}"]
    locations = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8", "replace"))
    paths = {urlparse(url).path or "/" for url in locations}
    problems = [f"/sitemap.xml: {url} is not an absolute http(s) URL" for url in locations
                if urlparse(url).scheme not in ("http", "https")]
    problems += [f"/sitemap.xml does not list {path}" for path in PUBLIC_PATHS if path not in paths]
    problems += [f"/sitemap.xml lists private page {path}" for path in sorted(paths)
                 if path.startswith(SITEMAP_FORBIDDEN_PREFIXES)]
    return problems


def crawl(base_url: str, env: str, *, max_requests: int = 400, timeout: float = 10.0,
          allowed_link_hosts: tuple[str, ...] = ALLOWED_LINK_HOSTS) -> tuple[list[str], int]:
    base = base_url.rstrip("/")
    origin = _origin_of(base)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    problems: list[str] = []
    queue: deque[tuple[str, str]] = deque()
    seen: set[str] = set()
    fragments: list[tuple[str, str, str]] = []
    ids_by_path: dict[str, set[str]] = {}

    def enqueue(reference: str, referer: str) -> None:
        absolute, fragment = urldefrag(urljoin(base + referer if referer.startswith("/") else base + "/", reference))
        if _origin_of(absolute) != origin:
            return
        if fragment:
            fragments.append((referer, urlparse(absolute).path or "/", fragment))
        if absolute not in seen:
            seen.add(absolute)
            queue.append((absolute, referer))

    for path in (*PUBLIC_PATHS, "/login", "/signup", "/.well-known/security.txt"):
        enqueue(path, "(start)")
    requests = 0
    while queue and requests < max_requests:
        url, referer = queue.popleft()
        requests += 1
        path = urlparse(url).path or "/"
        status, final, headers, body = _fetch(opener, url, timeout)
        if status is None:
            problems.append(f"{path}: request failed: {final}")
            continue
        if status >= 400:
            problems.append(f"{path}: HTTP {status} (linked from {referer})")
            continue
        if _origin_of(final) != origin:
            problems.append(f"{path}: redirected off-origin to {final}")
            continue
        final_path = urlparse(final).path or "/"
        content_type = (headers.get("content-type") or [""])[0].lower()
        if "text/html" in content_type:
            html = body.decode("utf-8", "replace")
            problems += [f"{final_path}: {p}" for p in check_csp(headers.get("content-security-policy", []))]
            problems += [f"{final_path}: {p}" for p in check_page(html, kind=path_kind(final_path), env=env, origin=origin,
                                                                 allowed_link_hosts=allowed_link_hosts)]
            page = ParsedPage(html)
            page_ids = {e.attrs["id"] for e in page.elements if "id" in e.attrs}
            ids_by_path[path] = ids_by_path[final_path] = page_ids
            for e in page.elements:
                for attr in ("href", "src"):
                    value = e.attrs.get(attr, "").strip()
                    if not value or value.startswith("#") or value.lower().startswith(("mailto:", "javascript:", "data:")):
                        continue
                    enqueue(value, final_path)
        elif "text/css" in content_type:
            for reference in re.findall(r"url\(\s*['\"]?([^'\")]+)", body.decode("utf-8", "replace")):
                if reference.startswith("data:"):
                    continue
                if not _same_origin(reference, origin):
                    problems.append(f"{final_path}: off-origin url() {reference}")
                else:
                    enqueue(reference, final_path)
        elif final_path == "/.well-known/security.txt" and b"Contact:" not in body:
            problems.append("/.well-known/security.txt has no Contact: line")
    if queue:
        problems.append(f"crawl stopped after {max_requests} requests with {len(queue)} URLs left; raise --max-requests")
    for referer, target, fragment in fragments:
        if target in ids_by_path and fragment not in ids_by_path[target]:
            problems.append(f"{referer}: link to {target}#{fragment}, but that page has no id {fragment!r}")
    # One line per CSP problem instead of one per page: the header is usually set site-wide.
    csp_pages: dict[str, list[str]] = {}
    other_problems: list[str] = []
    for problem in problems:
        path, _, issue = problem.partition(": ")
        if issue.startswith("CSP ") or issue == "no Content-Security-Policy header":
            csp_pages.setdefault(issue, []).append(path)
        else:
            other_problems.append(problem)
    problems = [f"{issue} on {len(paths)} page(s), for example {', '.join(paths[:3])}"
                for issue, paths in csp_pages.items()] + other_problems
    problems += check_sitemap(opener, base, timeout)
    problems += check_robots(opener, base, env, timeout)
    return list(dict.fromkeys(problems)), requests


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--templates", action="store_true", help="render every template with the sample contexts")
    mode.add_argument("--base-url", help="crawl a running server, for example http://127.0.0.1:8080")
    parser.add_argument("--env", default="development", choices=("production", "development", "e2e"),
                        help="environment the server runs in (crawl mode)")
    parser.add_argument("--web-dir", default=str(DEFAULT_WEB_DIR), help="website folder (templates mode)")
    parser.add_argument("--max-requests", type=int, default=400)
    parser.add_argument("--private-names", metavar="PATH",
                        help="names the pages must not carry (default: scripts/release_private_patterns.json, if it exists)")
    args = parser.parse_args(argv)
    names = load_private_names(args.private_names)
    if args.templates:
        problems, count = check_templates(Path(args.web_dir).resolve())
        scope = f"{count} renders of {len(template_names(Path(args.web_dir).resolve()))} templates"
    else:
        problems, count = crawl(args.base_url, args.env, max_requests=args.max_requests)
        scope = f"{count} requests to {args.base_url}, env {args.env}"
    scope += f"; {names} private names" if names else "; no private names file"
    if problems:
        print(f"SITE CHECK: FAIL ({len(problems)} problems; {scope})")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"SITE CHECK: PASS ({scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
