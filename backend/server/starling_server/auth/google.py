"""Sign in with Google: OpenID Connect, authorization code flow with PKCE (S256).

1. `/auth/google` sends the browser to Google with a fresh `state`, `nonce` and PKCE challenge.
   What the return trip needs travels in one signed, HttpOnly cookie that lives ten minutes, so
   nothing is written to the database before Google answers and an abandoned attempt leaves
   nothing behind.
2. Google sends the browser back to `/auth/google/callback` with a one-time code. `state` must
   match the cookie (so nobody can finish a sign-in in someone else's browser), the code is swapped
   for an ID token directly with Google over TLS (client secret plus the PKCE verifier), and the
   token is verified here in full: RS256 signature against Google's published keys, issuer,
   audience, authorized party, expiry, issue time and the nonce.
3. Only then is the address looked at, and only if Google says it is verified.

How far the address is trusted follows Google's own guidance. Google is AUTHORITATIVE for Gmail
addresses and for Google Workspace accounts (the `hd` claim): the person is the address's owner
now. For any other address `email_verified` only says Google checked it once, when that Google
account was made, so an existing Starling account on such an address is joined to Google only
after its password is entered (`auth.service.google_sign_in`).

Only the `openid email` scopes are asked for: Starling needs an address and nothing else about
the person's Google account.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..config import Settings
from ..security import clock
from ..security.crypto import b64url, b64url_decode, equals, random_token
from ..web import flash
from . import accounts

log = logging.getLogger(__name__)

AUTHORIZE_ORIGIN = "https://accounts.google.com"
AUTHORIZE_URL = AUTHORIZE_ORIGIN + "/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid email"

CALLBACK_PATH = "/auth/google/callback"
INTENTS = ("login", "signup", "link", "reauth")
START_TTL_S = 600
PENDING_TTL_S = 600
CLOCK_SKEW_S = 300
HTTP_TIMEOUT_S = 10.0
JWKS_MAX_AGE_S = 6 * 3600
JWKS_MIN_REFRESH_S = 60
MAX_TOKEN_LENGTH = 8192
MAX_SUBJECT_LENGTH = 255


class GoogleError(Exception):
    """Google did not confirm the sign-in. `reason` is for the log only, never shown."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- endpoints


@dataclass(frozen=True)
class Endpoints:
    authorize: str
    token: str
    jwks: str
    issuers: tuple[str, ...]


def endpoints(settings: Settings) -> Endpoints:
    """Google's endpoints, or the e2e harness's stand-in (never in production)."""
    base = settings.e2e_oidc_base
    if base and not settings.is_production:
        return Endpoints(f"{base}/o/oauth2/v2/auth", f"{base}/token", f"{base}/oauth2/v3/certs", (base,))
    return Endpoints(AUTHORIZE_URL, TOKEN_URL, JWKS_URL, ISSUERS)


def authorize_origin(settings: Settings) -> str:
    """The origin the browser is redirected to; the site's `form-action` policy must name it."""
    parts = urlsplit(endpoints(settings).authorize)
    return f"{parts.scheme}://{parts.netloc}"


def redirect_uri(settings: Settings) -> str:
    return settings.public_url + CALLBACK_PATH


# --------------------------------------------------------------------------- the round trip


def start_cookie_name(settings: Settings) -> str:
    return "__Host-starling_google" if settings.cookie_secure else "starling_google"


def pending_cookie_name(settings: Settings) -> str:
    return "__Host-starling_google_pending" if settings.cookie_secure else "starling_google_pending"


@dataclass(frozen=True)
class Start:
    """What `/auth/google` remembers for the callback (inside the signed cookie)."""

    state: str
    nonce: str
    verifier: str
    intent: str
    next: str
    account_id: str | None = None  # "link" and "reauth": the signed-in account that asked

    def payload(self) -> dict[str, Any]:
        return {"s": self.state, "n": self.nonce, "v": self.verifier, "i": self.intent, "x": self.next, "a": self.account_id}

    @classmethod
    def from_payload(cls, payload: Any) -> "Start | None":
        if not isinstance(payload, dict):
            return None
        values = [payload.get(key) for key in ("s", "n", "v", "i", "x")]
        if not all(isinstance(value, str) and value for value in values) or payload["i"] not in INTENTS:
            return None
        account_id = payload.get("a")
        return cls(*values, account_id=account_id if isinstance(account_id, str) else None)


def challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def begin(settings: Settings, *, intent: str, next_path: str, account_id: str | None = None,
          login_hint: str | None = None) -> tuple[str, str]:
    """Start a round trip: (the Google URL to send the browser to, the value of the start cookie)."""
    if intent not in INTENTS:
        raise ValueError(f"unknown intent {intent!r}")
    start = Start(random_token(), random_token(), random_token(), intent, next_path, account_id)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": SCOPE,
        "state": start.state,
        "nonce": start.nonce,
        "code_challenge": challenge(start.verifier),
        "code_challenge_method": "S256",
        # Always show the account chooser: someone signed in to two Google accounts must be able to
        # pick, and a shared computer must not sign the next person in as the last one.
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = endpoints(settings).authorize + "?" + urlencode(params)
    return url, flash.seal(settings, "google_start", start.payload(), START_TTL_S)


def read_start(settings: Settings, cookie_value: str | None, state: object) -> Start | None:
    """The start this callback belongs to, or None (missing, expired, altered or another state)."""
    start = Start.from_payload(flash.unseal(settings, "google_start", cookie_value))
    if start is None or not isinstance(state, str) or not equals(start.state, state):
        return None
    return start


# --------------------------------------------------------------------------- the person Google vouches for


@dataclass(frozen=True)
class GoogleUser:
    subject: str
    email: str
    email_verified: bool
    hosted_domain: str | None = None

    @property
    def authoritative(self) -> bool:
        """Google vouches that this person owns the address NOW: Gmail, or a Workspace account.

        Google's documented rule, exactly ("Verify the Google ID token on your server side"): a
        gmail.com address, or `email_verified` with `hd` set. `hd` is deliberately NOT compared with
        the address's domain: it names the organization's primary domain, and a Workspace address
        may sit on one of its verified secondary domains. Google issues Workspace addresses only on
        domains the organization has proved it owns, so a mismatch is an ordinary account, not a
        forgery; demanding a match would only send those people to the password step. (Security
        review 2026-09-22 raised it; kept on this reasoning. A forged token fails the signature.)
        """
        domain = self.email.rpartition("@")[2].lower()
        return self.email_verified and (domain in accounts.GMAIL_DOMAINS or bool(self.hosted_domain))

    def pending(self, kind: str, next_path: str) -> dict[str, Any]:
        return {"k": kind, "sub": self.subject, "email": self.email, "auth": self.authoritative,
                "hd": self.hosted_domain, "x": next_path}


@dataclass(frozen=True)
class Pending:
    """A Google sign-in that needs one more step: create the account ("new") or enter the password ("link")."""

    kind: str
    user: GoogleUser
    next: str


def seal_pending(settings: Settings, user: GoogleUser, kind: str, next_path: str) -> str:
    return flash.seal(settings, "google_pending", user.pending(kind, next_path), PENDING_TTL_S)


def read_pending(settings: Settings, cookie_value: str | None, kind: str) -> Pending | None:
    payload = flash.unseal(settings, "google_pending", cookie_value)
    if not isinstance(payload, dict) or payload.get("k") != kind:
        return None
    subject, email, next_path = payload.get("sub"), payload.get("email"), payload.get("x")
    if not all(isinstance(value, str) and value for value in (subject, email, next_path)):
        return None
    hosted = payload.get("hd")
    user = GoogleUser(subject, email, True, hosted if isinstance(hosted, str) and hosted else None)
    return Pending(kind, user, next_path)


# --------------------------------------------------------------------------- talking to Google


_SSL_CONTEXT = None


def _tls():
    """One verified TLS context for every call to Google. Building one loads the whole certificate
    store (~0.25 s of CPU on the event loop every customer shares), so it is built once."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT


async def post_token(url: str, data: dict[str, str]) -> httpx.Response:
    # trust_env=False: a proxy variable in the environment must never see the client secret.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, trust_env=False, follow_redirects=False, verify=_tls()) as client:
        return await client.post(url, data=data, headers={"Accept": "application/json"})


async def get_json(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, trust_env=False, follow_redirects=False, verify=_tls()) as client:
        return await client.get(url, headers={"Accept": "application/json"})


async def exchange_code(settings: Settings, code: str, verifier: str) -> str:
    """Swap the one-time code for an ID token (the token endpoint checks the PKCE verifier)."""
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri(settings),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    try:
        response = await post_token(endpoints(settings).token, data)
    except httpx.HTTPError as exc:
        raise GoogleError(f"token endpoint unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise GoogleError(f"token endpoint answered {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise GoogleError("token endpoint answered with something that is not JSON") from exc
    token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise GoogleError("no id_token in the token response")
    return token


class _KeyCache:
    """Google's signing keys, by key id. Refetched when a token names a key it has not seen.

    No lock: two sign-ins racing past an empty cache fetch the keys twice, which is harmless, and
    a lock made at import time would belong to whichever event loop touched it first.
    """

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, rsa.RSAPublicKey]] = {}
        self.fetched_at: dict[str, float] = {}

    def clear(self) -> None:
        self.keys.clear()
        self.fetched_at.clear()


_KEYS = _KeyCache()


def _public_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey | None:
    if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256":
        return None
    try:
        n = int.from_bytes(b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(b64url_decode(jwk["e"]), "big")
    except (KeyError, TypeError, ValueError):
        return None
    if n.bit_length() < 2048:
        return None
    return rsa.RSAPublicNumbers(e, n).public_key()


async def _refresh_keys(url: str) -> None:
    try:
        response = await get_json(url)
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleError(f"signing keys unreachable: {type(exc).__name__}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise GoogleError("signing keys response has no keys")
    keys: dict[str, rsa.RSAPublicKey] = {}
    for jwk in body["keys"]:
        if isinstance(jwk, dict) and isinstance(jwk.get("kid"), str):
            key = _public_key(jwk)
            if key is not None:
                keys[jwk["kid"]] = key
    if not keys:
        raise GoogleError("signing keys response has no usable RSA key")
    _KEYS.keys[url] = keys
    _KEYS.fetched_at[url] = time.monotonic()


async def signing_key(settings: Settings, kid: str) -> rsa.RSAPublicKey:
    url = endpoints(settings).jwks
    age = time.monotonic() - _KEYS.fetched_at.get(url, float("-inf"))
    known = _KEYS.keys.get(url, {})
    if kid in known and age < JWKS_MAX_AGE_S:
        return known[kid]
    # Google rotates its keys every few days; an unknown key id means a fresh fetch, but never
    # more than once a minute, so a stream of forged tokens cannot turn into a stream of fetches.
    if age >= JWKS_MIN_REFRESH_S or not known:
        await _refresh_keys(url)
    key = _KEYS.keys.get(url, {}).get(kid)
    if key is None:
        raise GoogleError("the token is signed with a key Google does not publish")
    return key


def _segment(value: str) -> Any:
    try:
        return json.loads(b64url_decode(value))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GoogleError("the token is not well formed") from exc


def check_claims(settings: Settings, claims: Any, *, nonce: str, now: int) -> GoogleUser:
    """Every claim a relying party must check (OpenID Connect Core 3.1.3.7), plus Google's own."""
    if not isinstance(claims, dict):
        raise GoogleError("the token has no claims")
    if claims.get("iss") not in endpoints(settings).issuers:
        raise GoogleError("the token was issued by someone else")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.google_client_id not in audiences:
        raise GoogleError("the token was issued to another application")
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != settings.google_client_id:
        raise GoogleError("the token names another authorized party")
    expires, issued = claims.get("exp"), claims.get("iat")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires < now - CLOCK_SKEW_S:
        raise GoogleError("the token has expired")
    if not isinstance(issued, int) or isinstance(issued, bool) or issued > now + CLOCK_SKEW_S:
        raise GoogleError("the token was issued in the future")
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not equals(token_nonce, nonce):
        raise GoogleError("the token belongs to another sign-in")
    subject, email = claims.get("sub"), claims.get("email")
    if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
        raise GoogleError("the token has no subject")
    if not isinstance(email, str) or not email:
        raise GoogleError("the token has no email address")
    verified = claims.get("email_verified")
    hosted = claims.get("hd")
    return GoogleUser(
        subject=subject,
        email=email.strip(),
        email_verified=verified is True or verified == "true",
        hosted_domain=hosted if isinstance(hosted, str) and hosted else None,
    )


async def verify_id_token(settings: Settings, token: str, *, nonce: str, now: int | None = None) -> GoogleUser:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise GoogleError("the token is not well formed")
    header = _segment(parts[0])
    # Only RS256, whatever the token says: "none" and HS256 are how forged tokens get accepted.
    if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise GoogleError("the token is not signed the way Google signs")
    key = await signing_key(settings, header["kid"])
    try:
        signature = b64url_decode(parts[2])
        key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, UnicodeEncodeError) as exc:
        raise GoogleError("the token's signature is wrong") from exc
    return check_claims(settings, _segment(parts[1]), nonce=nonce, now=clock.now() if now is None else now)


async def finish(settings: Settings, start: Start, code: object) -> GoogleUser:
    """The person Google vouches for, or GoogleError."""
    if not isinstance(code, str) or not code or len(code) > 2048:
        raise GoogleError("no code in the callback")
    token = await exchange_code(settings, code, start.verifier)
    return await verify_id_token(settings, token, nonce=start.nonce)
