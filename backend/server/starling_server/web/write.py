"""The writing page: Starling on a phone, where the Chrome extension cannot run.

A signed-in person pastes the post they want to answer (or its link, or shares it from the X app
on Android), gets a draft in their own voice from their own engine, copies it or opens it in X,
and presses Post themselves. Same account, same voice, same plan, same limits as the extension:
drafts go through `api.engine_routes.metered_assist`, the exact path of `POST /api/v1/assist`
(reservation, rate limits, fair use, pause, the account's one request slot, settlement, stored
result), into the engine's composer pipeline (`enhance_composer`, intent `create`), which is
what the extension's Create reply / quote / post buttons run.

| Route | What it does |
|---|---|
| `GET /write` | `write/index.html`; share target: `title`, `text`, `url` pre-fill the box |
| `POST /write/consent` | records consent kind `web_disclosure` at `WEB_DISCLOSURE_VERSION` (form) |
| `POST /write/consent/withdraw` | revokes it (form) |
| `POST /write/link` | JSON: the post behind a link to X, read by the services process through X's oEmbed |
| `POST /write/draft` | JSON: one metered draft (`Idempotency-Key` header, as the extension's API) |
| `GET /write/requests/{key}` | JSON: `pending` (202), `done` (200) or 404, for a phone that lost the connection |
| `GET /write/voice` | `write/voice.html`: the writing samples (engine `/own-material/list`, kind voice) |
| `POST /write/voice/add`, `POST /write/voice/{id}/remove` | forms; back to the page with a message |
| `GET /manifest.webmanifest` | the installable web app, with an Android share target |
| `GET /sw.js` | the service worker: the offline page and nothing else |
| `GET /offline` | `offline.html` |

Every POST passes the CSRF token (form field, or `X-CSRF-Token` on the JSON routes) and the
origin check. The JSON routes answer refusals with the error body of CONTRACTS 6.3.

Consent: drafting sends what the person pastes to Starling's servers and AI provider, so before
anything is sent the page shows `DISCLOSURE` once and records the answer in `consents` (kind
`web_disclosure`, version `WEB_DISCLOSURE_VERSION`), the way the extension records its own
disclosure. The web version is separate from `STARLING_CONSENT_VERSION` because that one is
compiled into the extension: the two texts change independently. A test pins the text to
`WEB_DISCLOSURE_SHA256`, so a changed word without a new version fails.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from ..api import engine_routes
from ..auth.deps import CurrentUser, current_user_async, require_user, verify_csrf
from ..db import request_errors
from ..engines.pool import clean_time_zone
from ..engines.streams import is_usable
from ..security import clock, ratelimit
from ..security.errors import ApiError, PageError, StarlingRoute, WebRedirect
from ..security.http import form_str, read_form, read_json_object, safe_next, wants_json
from ..services import client as services_client
from ..services.world.x_post import parse_status_url, snowflake_time
from ..usage import service as usage_service
from . import flash
from .render import account_state, render

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

WEB_CONSENT_KIND = usage_service.WEB_CONSENT_KIND
WEB_DISCLOSURE_VERSION = "2026-10-31.1"
WEB_DISCLOSURE_SHA256 = "23aedaea076e7909fd874b4f69cf01f73a8f3e05511c96062350de3527b383dc"
WEB_CONSENT = (WEB_CONSENT_KIND, WEB_DISCLOSURE_VERSION)

DISCLOSURE: dict[str, Any] = {
    "title": "Before your first draft",
    "intro": "Starling writes drafts with its hosted AI, so what you give it here is sent to Starling's "
             "servers. Nothing is sent until you agree.",
    "sections": [
        {
            "heading": "What Starling sends",
            "items": [
                "The post you paste or share: its words, the author's name or handle if you add it, and its link.",
                "When you give a link to a post on X, Starling's server asks X's public embed service for "
                "that post's words and author. X gets the link, not your account details.",
                "For a new post, what you say it is about.",
                "Posts of your own that you add as writing samples.",
                "Links in the post, which Starling's servers may open to read the page.",
                "Only if Starling's web search is turned on (it is off today): short web searches, sent to "
                "the AI provider's web search: the subjects you write about, to find what is new in them "
                "(topic names only, never your own text), and, when a post makes a claim about recent "
                "events, a search built from that post.",
                "Your account and usage information.",
            ],
        },
        {
            "heading": "Who receives it",
            "items": [
                "Starling's servers, operated by the service owner.",
                "Starling's AI provider, which writes the drafts.",
                "X, only the link to a post you give Starling.",
            ],
        },
        {
            "heading": "What Starling promises",
            "items": [
                "Your information is not sold and is not used for advertising.",
                "You can export or delete your data at any time from the Data page of your account.",
                "Starling never posts for you. You copy the draft or open it in X, and you press Post.",
            ],
        },
    ],
    "agree": "Agree and continue",
    "decline": "Not now",
}

KINDS = ("reply", "quote", "post")
DRAFT_ROUTE = "/write/draft"
DRAFT_MODE = "enhance_composer"
MAX_BODY_BYTES = 64 * 1024
MAX_POST_TEXT = 10_000
MAX_SUBJECT = 500
MAX_AUTHOR = 100
MAX_AUTHOR_NAME = 200
MAX_LINKS = 4
MAX_SAMPLE = 2_000
MAX_PREFILL = 4_000
# A hand-off to X carries the draft in its address. Past this the link is left out and Copy remains.
MAX_INTENT_URL = 8_000
X_INTENT = "https://x.com/intent/post"
X_POST_LOOKUP_PER_ACCOUNT = ratelimit.Rule("x_post_lookup", 30, 600)
NO_STORE = {"Cache-Control": "no-store"}

_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_URL_RE = re.compile(r"https?://[^\s<>\"']{4,2000}")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SKIPPED_LINK_HOSTS = ("x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com",
                       "pic.twitter.com", "pic.x.com")


def disclosure_text() -> str:
    """The text `WEB_DISCLOSURE_SHA256` covers, in display order."""
    lines = [DISCLOSURE["title"], DISCLOSURE["intro"]]
    for section in DISCLOSURE["sections"]:
        lines.append(section["heading"])
        lines.extend(section["items"])
    lines += [DISCLOSURE["agree"], DISCLOSURE["decline"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- consent


def _consent_row(request: Request, account_id: str) -> dict[str, Any] | None:
    with request.app.state.db.read() as conn:
        row = conn.execute(
            "SELECT granted_at FROM consents WHERE account_id = ? AND kind = ? AND version = ? AND revoked_at IS NULL",
            (account_id, WEB_CONSENT_KIND, WEB_DISCLOSURE_VERSION),
        ).fetchone()
    return {"granted_at": int(row["granted_at"])} if row is not None else None


def _consent_context(request: Request, user: CurrentUser) -> dict[str, Any]:
    row = _consent_row(request, user.account_id)
    return {"given": row is not None, "granted_at": row["granted_at"] if row else None,
            "version": WEB_DISCLOSURE_VERSION}


def _next_path(value: str | None) -> str:
    target = safe_next(value, "/write")
    return target if target == "/write" or target.startswith(("/write?", "/write/voice")) else "/write"


def _here(request: Request) -> str:
    query = request.url.query
    return _next_path(request.url.path + (f"?{query}" if query else ""))


async def page_user(request: Request) -> CurrentUser:
    """The signed-in person, or a trip to sign in that comes back here (shared text included)."""
    user = await current_user_async(request)
    if user is None:
        raise WebRedirect("/login?next=" + quote(_here(request), safe=""))
    return user


@router.post("/write/consent", dependencies=[Depends(verify_csrf)])
async def give_consent(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    form = await read_form(request)
    if form_str(form, "agree") != "yes":
        raise PageError(400, "Press Agree and continue to agree.")
    now = clock.now()
    await request.app.state.db.write_async(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at, revoked_at) "
        "VALUES (?, ?, ?, 0, ?, NULL) ON CONFLICT (account_id, kind, version) DO UPDATE SET "
        "granted_at = excluded.granted_at, revoked_at = NULL",
        (user.account_id, WEB_CONSENT_KIND, WEB_DISCLOSURE_VERSION, now),
    ))
    log.info("web consent recorded account=%s", user.account_id)
    return RedirectResponse(_next_path(form_str(form, "next", 2048)), status_code=303, headers=NO_STORE)


@router.post("/write/consent/withdraw", dependencies=[Depends(verify_csrf)])
async def withdraw_consent(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    now = clock.now()
    await request.app.state.db.write_async(lambda conn: conn.execute(
        "UPDATE consents SET revoked_at = ? WHERE account_id = ? AND kind = ? AND revoked_at IS NULL",
        (now, user.account_id, WEB_CONSENT_KIND),
    ))
    log.info("web consent withdrawn account=%s", user.account_id)
    response = RedirectResponse("/write", status_code=303, headers=NO_STORE)
    flash.set_flash(response, request.app.state.settings, "info",
                    "You withdrew your OK. Starling sends nothing from this page until you agree again.")
    return response


def _require_web_consent(request: Request, user: CurrentUser) -> None:
    if _consent_row(request, user.account_id) is None:
        raise ApiError("consent_required", 403, engine_routes.CONSENT_MESSAGES[WEB_CONSENT_KIND])


# --------------------------------------------------------------------------- the page


def _clip(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def prefill_from_share(params: dict[str, str]) -> dict[str, Any]:
    """What a share from another app (Android's share sheet, `share_target` in the manifest) put in
    the address: the title, text and url parameters joined once each, and any post link found."""
    parts: list[str] = []
    for name in ("title", "text", "url"):
        value = _clip(params.get(name), MAX_PREFILL)
        if value and not any(value in part for part in parts):
            parts = [part for part in parts if part not in value]
            parts.append(value)
    text = "\n".join(parts)[:MAX_PREFILL]
    link = parse_status_url(text)
    return {"text": text, "link": link.url if link else None, "shared": bool(text)}


@router.get("/write")
async def write_page(request: Request, user: CurrentUser = Depends(page_user)) -> Response:
    state = await _state(request, user)
    return await render(request, "write/index.html", {
        "state": state,
        "consent": _consent_context(request, user),
        "disclosure": DISCLOSURE,
        "prefill": prefill_from_share(dict(request.query_params)),
        "next_path": _here(request),
    })


async def _state(request: Request, user: CurrentUser) -> dict[str, Any]:
    try:
        return await account_state(request, user.account_id)
    except usage_service.UsageError as exc:
        raise PageError(exc.http_status if 400 <= exc.http_status < 500 else 503, exc.message) from exc


# --------------------------------------------------------------------------- reading a link


@router.post("/write/link", dependencies=[Depends(verify_csrf)])
async def read_link(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    """The post behind a link to X, so a phone that can only copy links still gets the words."""
    body = await read_json_object(request, 8 * 1024)
    link = parse_status_url(body.get("url"))
    if link is None:
        raise ApiError("bad_request", 400, "That is not a link to a post on X.")
    # The same gate as a draft: only an active, confirmed account that agreed can make the server ask X.
    if user.status != "active":
        raise ApiError("account_suspended", 403, "This account is suspended. Contact support for help.")
    if not user.email_verified:
        raise ApiError("email_unverified", 403, "Verify your email address to start drafting.")
    _require_web_consent(request, user)
    limit = await ratelimit.check(request.app.state.db, [(X_POST_LOOKUP_PER_ACCOUNT, ratelimit.account_key(user.account_id))])
    if not limit.allowed:
        raise ApiError("rate_limited", 429, "That is a lot of links in a short time. Paste the post's words instead.",
                       retry_after_s=limit.retry_after_s)
    answer = await services_client.read_x_post(request.app.state.settings, link.url)
    error = answer.get("error") if answer.get("ok") is False else None
    if error is not None or not isinstance(answer.get("status_id"), str):
        code = error.get("code") if isinstance(error, dict) else None
        log.info("x post lookup failed account=%s code=%s", user.account_id, code or "unknown")
        message = ("X does not show that post outside X. Paste its text instead." if code == "post_unavailable"
                   else "Couldn't read that post. Paste its text instead.")
        return JSONResponse({"ok": False, "link": link.url, "status_id": link.status_id, "message": message},
                            headers=NO_STORE)
    handle = answer.get("author_handle") if isinstance(answer.get("author_handle"), str) else link.handle
    post = {
        "url": str(answer.get("url") or link.url),
        "status_id": link.status_id,
        "author_name": _clip(answer.get("author_name") if isinstance(answer.get("author_name"), str) else "", MAX_AUTHOR_NAME),
        "author_handle": handle,
        "text": _clip(answer.get("text") if isinstance(answer.get("text"), str) else "", MAX_POST_TEXT),
        "lang": answer.get("lang") if isinstance(answer.get("lang"), str) and _LANG_RE.match(answer["lang"]) else None,
        "has_media": bool(answer.get("has_media")),
    }
    if not post["text"]:
        return JSONResponse({"ok": False, "link": post["url"], "status_id": link.status_id, "post": post,
                             "message": "That post has no words Starling can read. Paste or describe it instead."},
                            headers=NO_STORE)
    return JSONResponse({"ok": True, "post": post}, headers=NO_STORE)


# --------------------------------------------------------------------------- drafting


def _links_in(text: str) -> list[str]:
    links: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].split(":", 1)[0].lower()
        if host in _SKIPPED_LINK_HOSTS or url in links:
            continue
        links.append(url)
        if len(links) >= MAX_LINKS:
            break
    return links


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def build_request(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(engine request, what the page needs to hand the draft to X) from the page's JSON.

    The engine request is the extension's Create reply / quote / post (`enhance_composer`, intent
    `create`). A pasted post honestly provides its words, maybe its author and link (and from the
    link its id, its creation time and, after a lookup, its language); every other field the
    extension reads off x.com is left out, which the engine's `PostContext` accepts.
    """
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ApiError("bad_request", 400, "Choose Reply, Quote or New post.")
    raw_text = payload.get("text")
    if raw_text is not None and not isinstance(raw_text, str):
        raise ApiError("bad_request", 400, "The post must be text.")
    text = (raw_text or "").replace("\r\n", "\n").strip()
    again = payload.get("again") is True
    if kind == "post":
        subject = " ".join(text.split())
        if len(subject) > MAX_SUBJECT:
            raise ApiError("bad_request", 400, f"Keep what it is about under {MAX_SUBJECT} characters.")
        composer: dict[str, Any] = {"action": "post", "draft_text": "", "intent": "create"}
        if subject:
            composer["topic"] = subject
        return {"mode": DRAFT_MODE, "composer": composer}, {"kind": kind, "status_id": None, "url": None}

    link = parse_status_url(payload.get("link")) if isinstance(payload.get("link"), str) else None
    if link is None:
        link = parse_status_url(text)
    if link is not None:
        # The link travels on its own; the words are what the post says.
        text = re.sub(r"[ \t]*\S*(?:x|twitter)\.com/\S*status(?:es)?/0*" + re.escape(link.status_id) + r"\S*[ \t]*",
                      " ", text).strip()
    if not text:
        message = ("Paste the post's words too. X's link alone is not enough." if link is not None
                   else "Paste the post you're answering.")
        raise ApiError("bad_request", 400, message)
    if len(text) > MAX_POST_TEXT:
        raise ApiError("bad_request", 400, f"That post is too long. Keep it under {MAX_POST_TEXT:,} characters.")
    author_raw = payload.get("author")
    author_raw = author_raw.strip() if isinstance(author_raw, str) else ""
    if len(author_raw) > MAX_AUTHOR:
        raise ApiError("bad_request", 400, "That author name is too long.")
    handle_match = _HANDLE_RE.match(author_raw)
    handle = handle_match.group(1) if handle_match else (link.handle if link else None)
    author_name = payload.get("author_name")
    author_name = " ".join(author_name.split())[:MAX_AUTHOR_NAME] if isinstance(author_name, str) else ""
    if author_raw and not handle_match:
        author_name = author_raw  # a name, not a handle
    context: dict[str, Any] = {"author": author_name, "text": text, "visibility": "unknown",
                               "links": _links_in(text), "media": []}
    if handle:
        context["author_handle"] = handle
    lang = payload.get("lang")
    if isinstance(lang, str) and _LANG_RE.match(lang):
        context["language"] = lang
    if link is not None:
        context["post_id"] = link.status_id
        context["post_url"] = link.url
        created = _iso(snowflake_time(link.status_id, now=clock.now()))
        if created:
            context["post_created_at"] = created
    engine = {"mode": DRAFT_MODE, "composer": {"action": kind, "draft_text": "", "intent": "create"},
              "context": context}
    if again and kind == "reply":
        engine["second_reply_ok"] = True
    return engine, {"kind": kind, "status_id": link.status_id if link else None, "url": link.url if link else None}


def handoff(kind: str, draft: str, status_id: str | None, url: str | None) -> dict[str, str] | None:
    """The one-tap link that opens X's post box with the draft filled in. The person still presses
    Post. Only when X will put it in the right place: a reply needs the post's id, a quote its link."""
    if kind == "reply" and status_id:
        params = {"in_reply_to": status_id, "text": draft}
        label = "Reply on X"
    elif kind == "quote" and url:
        params = {"url": url, "text": draft}
        label = "Quote on X"
    elif kind == "post":
        params = {"text": draft}
        label = "Post on X"
    else:
        return None
    link = f"{X_INTENT}?{urlencode(params, quote_via=quote)}"
    return {"label": label, "url": link} if len(link) <= MAX_INTENT_URL else None


def _page_result(answer: Any, target: dict[str, Any], key: str) -> dict[str, Any]:
    """The engine's answer, reduced to what the page shows."""
    response = answer if isinstance(answer, dict) else {}
    decision = response.get("decision") if isinstance(response.get("decision"), dict) else {}
    usage = response.get("usage")
    draft = decision.get("draft") if isinstance(decision.get("draft"), str) else ""
    usable = is_usable(response)
    result: dict[str, Any] = {"ok": True, "request_id": key, "kind": target["kind"], "usage": usage, "draft": None}
    if response.get("cancelled"):
        result.update(ok=False, message="Stopped. No draft was used.")
        return result
    if not usable:
        message = response.get("error") if isinstance(response.get("error"), str) and response.get("error") else None
        message = message or (decision.get("rationale") if isinstance(decision.get("rationale"), str) else None)
        result["message"] = message or "Starling came back without a draft. Try again."
        result["can_write_anyway"] = bool(decision.get("already_replied")) and target["kind"] == "reply"
        reasons = decision.get("reasons")
        if isinstance(reasons, list):
            result["reasons"] = [str(item) for item in reasons[:2] if isinstance(item, str) and item.strip()]
        return result
    parts = decision.get("thread_parts")
    parts = [str(part) for part in parts if isinstance(part, str) and part.strip()] if isinstance(parts, list) else []
    result["draft"] = draft.strip()
    result["parts"] = parts if len(parts) > 1 else []
    result["handoff"] = None if result["parts"] else handoff(target["kind"], result["draft"], target["status_id"],
                                                             target["url"])
    for field in ("voice_note", "review_note", "search_note"):
        value = decision.get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()
    if decision.get("needs_review") and "review_note" not in result:
        rationale = decision.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            result["review_note"] = rationale.strip()
    sources = decision.get("sources")
    if isinstance(sources, list):
        result["sources"] = [
            {"url": item["url"], "title": str(item.get("title") or item["url"])[:200]}
            for item in sources[:3]
            if isinstance(item, dict) and isinstance(item.get("url"), str)
            and item["url"].startswith(("https://", "http://"))
        ]
    return result


def _idempotency_key(request: Request) -> str:
    raw = (request.headers.get("idempotency-key") or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise ApiError("bad_request", 400, "Reload the page and try again.") from exc
    if str(parsed) != raw.lower():
        raise ApiError("bad_request", 400, "Reload the page and try again.")
    return str(parsed)


def _record_failure(request: Request, user: CurrentUser, exc: ApiError) -> None:
    """Failures of the metered web route count on the owner dashboard like the extension's."""
    request_errors.record(
        request.app.state.db, route=DRAFT_ROUTE, code=exc.code, http_status=exc.status_code, now=clock.now(),
        account_id=user.account_id, mode=DRAFT_MODE,
        reserved=getattr(request.state, "reservation_id", None) is not None)


@router.post(DRAFT_ROUTE, dependencies=[Depends(verify_csrf)])
async def write_draft(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    if not wants_json(request):
        # The page drafts with JavaScript; a plain form post lands back on it with a word about why.
        response = RedirectResponse("/write", status_code=303, headers=NO_STORE)
        flash.set_flash(response, request.app.state.settings, "warning",
                        "Writing a draft here needs JavaScript. Turn it on and try again.")
        return response
    try:
        payload = await read_json_object(request, MAX_BODY_BYTES)
        engine_request, target = build_request(payload)
        key = _idempotency_key(request)
        body = json.dumps(engine_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        answer = await engine_routes.metered_assist(
            request, user, body, mode=DRAFT_MODE, key=key,
            time_zone=clean_time_zone(request.headers.get("x-starling-time-zone")),
            route="/write", consent=WEB_CONSENT)
    except ApiError as exc:
        _record_failure(request, user, exc)
        raise
    return JSONResponse(_page_result(answer, target, key), headers=NO_STORE)


@router.get("/write/requests/{key}")
async def write_request_state(key: str, request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    """Whether a draft the page lost track of is still being written, or is ready to collect (the
    page then sends the same request again with the same key and gets the stored draft, uncharged)."""
    try:
        parsed = str(uuid.UUID(key))
    except ValueError as exc:
        raise ApiError("not_found", 404, "There is no stored result for this request.") from exc
    status, state = await engine_routes.stored_result(request, user.account_id, parsed)
    return JSONResponse({"request_id": parsed, "state": state["state"]}, status_code=status, headers=NO_STORE)


# --------------------------------------------------------------------------- writing samples


def _sample_view(item: dict[str, Any]) -> dict[str, Any] | None:
    item_id = item.get("id")
    text = item.get("text")
    if not isinstance(item_id, str) or not _ITEM_ID_RE.match(item_id) or not isinstance(text, str):
        return None
    created = item.get("created_at")
    added_at = None
    if isinstance(created, str):
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            added_at = int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
        except ValueError:
            added_at = None
    return {"id": item_id, "text": text, "added_at": added_at}


async def _voice_page(request: Request, user: CurrentUser, *, errors: dict[str, str] | None = None,
                      form: dict[str, str] | None = None, status_code: int = 200) -> Response:
    state = await _state(request, user)
    consent = _consent_context(request, user)
    samples: list[dict[str, Any]] | None = None
    limits = {"max_chars": MAX_SAMPLE, "min_words": 3, "max_items": 200}
    unavailable = None
    if consent["given"] and state["account"]["email_verified"] and state["account"]["status"] == "active":
        try:
            listing = await engine_routes.forward_unmetered(
                request, user, "/own-material/list", {"kind": "voice"}, consent=WEB_CONSENT)
            items = listing.get("items") if isinstance(listing.get("items"), list) else []
            samples = [view for view in (_sample_view(item) for item in items if isinstance(item, dict)) if view]
            raw_limits = listing.get("limits") if isinstance(listing.get("limits"), dict) else {}
            limits = {
                "max_chars": int(raw_limits.get("max_chars") or MAX_SAMPLE),
                "min_words": int(raw_limits.get("min_voice_words") or 3),
                "max_items": int(raw_limits.get("max_items") or 200),
            }
        except ApiError as exc:
            log.info("writing samples unavailable account=%s code=%s", user.account_id, exc.code)
            unavailable = exc.message if exc.code in ("rate_limited", "busy", "email_unverified", "account_suspended") \
                else "Starling can't load your writing samples right now. Try again in a moment."
    return await render(request, "write/voice.html", {
        "state": state, "consent": consent, "disclosure": DISCLOSURE, "samples": samples, "limits": limits,
        "errors": errors or {}, "form": form or {}, "unavailable": unavailable, "next_path": "/write/voice",
    }, status_code=status_code)


@router.get("/write/voice")
async def voice_page(request: Request, user: CurrentUser = Depends(page_user)) -> Response:
    return await _voice_page(request, user)


@router.post("/write/voice/add", dependencies=[Depends(verify_csrf)])
async def voice_add(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    form = await read_form(request)
    text = (form_str(form, "text") or "").replace("\r\n", "\n").strip()
    if not text:
        return await _voice_page(request, user, errors={"text": "Paste one of your own posts first."}, status_code=400)
    if len(text) > MAX_SAMPLE:
        return await _voice_page(request, user, form={"text": text[:MAX_PREFILL]}, status_code=400,
                                 errors={"text": f"That is {len(text):,} characters. Keep one sample under {MAX_SAMPLE:,}."})
    try:
        await engine_routes.forward_unmetered(request, user, "/own-material/add", {"kind": "voice", "text": text},
                                              consent=WEB_CONSENT)
    except ApiError as exc:
        if exc.code == "bad_request":  # the engine's own sentence ("A voice example needs at least 3 words ...")
            return await _voice_page(request, user, errors={"text": exc.message}, form={"text": text}, status_code=400)
        if exc.code == "consent_required":
            return RedirectResponse("/write/voice", status_code=303, headers=NO_STORE)
        return await _voice_page(request, user, errors={"form": _sample_failure(exc)}, form={"text": text},
                                 status_code=exc.status_code if 400 <= exc.status_code < 600 else 503)
    response = RedirectResponse("/write/voice", status_code=303, headers=NO_STORE)
    flash.set_flash(response, request.app.state.settings, "success", "Added. Starling learns from it on your next draft.")
    return response


@router.post("/write/voice/{item_id}/remove", dependencies=[Depends(verify_csrf)])
async def voice_remove(item_id: str, request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    if not _ITEM_ID_RE.match(item_id):
        raise PageError(404, "That sample is already gone.")
    response = RedirectResponse("/write/voice", status_code=303, headers=NO_STORE)
    try:
        await engine_routes.forward_unmetered(request, user, "/own-material/remove", {"id": item_id}, consent=WEB_CONSENT)
    except ApiError as exc:
        if exc.code == "not_found":
            flash.set_flash(response, request.app.state.settings, "info", "That sample was already gone.")
            return response
        if exc.code == "consent_required":
            return response
        return await _voice_page(request, user, errors={"form": _sample_failure(exc)},
                                 status_code=exc.status_code if 400 <= exc.status_code < 600 else 503)
    flash.set_flash(response, request.app.state.settings, "success", "Removed.")
    return response


def _sample_failure(exc: ApiError) -> str:
    if exc.code in ("rate_limited", "busy", "email_unverified", "account_suspended"):
        return exc.message
    return "Starling couldn't save that right now. Try again in a moment."


# --------------------------------------------------------------------------- installable app


def _static_url(request: Request, path: str) -> str:
    target = Path(request.app.state.settings.web_dir) / "static" / path
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={digest}"


def manifest(request: Request) -> dict[str, Any]:
    icon = lambda path, size, purpose: {"src": _static_url(request, path), "sizes": size, "type": "image/png",  # noqa: E731
                                        "purpose": purpose}
    return {
        "id": "/write",
        "name": "Starling",
        "short_name": "Starling",
        "description": "Drafts for X in your own voice. You copy them and post them yourself.",
        "lang": "en",
        "start_url": "/write",
        "scope": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            icon("img/starling-icon-192.png", "192x192", "any"),
            icon("img/starling-icon-512.png", "512x512", "any"),
            icon("img/starling-icon-maskable-512.png", "512x512", "maskable"),
        ],
        "shortcuts": [
            {"name": "Teach Starling your voice", "short_name": "Your voice", "url": "/write/voice"},
        ],
        # Android: "Share" from the X app (or anywhere) opens the writing page with the shared
        # text or link in the box. GET, so nothing is posted without the person pressing a button.
        "share_target": {"action": "/write", "method": "GET",
                         "params": {"title": "title", "text": "text", "url": "url"}},
    }


@router.get("/manifest.webmanifest")
async def web_manifest(request: Request) -> Response:
    return Response(json.dumps(manifest(request), indent=2), media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sw.js")
async def service_worker(request: Request) -> Response:
    path = Path(request.app.state.settings.web_dir) / "static" / "js" / "sw.js"
    if not path.is_file():
        raise PageError(404, "Not found.")
    # Served from the root so it may answer for the whole site; never cached long, so a new
    # version reaches every phone on its next visit.
    return FileResponse(path, media_type="text/javascript", headers={
        "Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/offline")
async def offline_page(request: Request) -> Response:
    return await render(request, "offline.html", {})
