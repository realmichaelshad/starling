"""A stand-in for Google's sign-in, for the e2e harness. Never used outside development and e2e.

The browser really goes here: the account chooser is a small page with an address field, and
choosing an account sends the browser back to the gateway's callback with a one-time code, as
Google does. The gateway then calls the token endpoint and the signing keys the way it calls
Google's (`STARLING_E2E_OIDC_BASE`, which production refuses). The ID tokens are real RS256 JWTs
signed with a key made at start-up, so the gateway's whole verification path runs: signature,
issuer, audience, nonce, expiry, and the PKCE check on the code.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _int_b64(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def subject_for(email: str) -> str:
    """A stable Google account id per address, so signing in again finds the same account."""
    return "e2e-" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:20]


def create_app(*, client_id: str, client_secret: str, base_url: str) -> FastAPI:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "e2e-" + secrets.token_hex(4)
    codes: dict[str, dict[str, Any]] = {}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def sign(claims: dict[str, Any]) -> str:
        header = _b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
        payload = _b64url(json.dumps(claims).encode())
        signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64url(signature)}"

    @app.get("/_control/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/o/oauth2/v2/auth")
    async def chooser(request: Request) -> HTMLResponse:
        q = request.query_params
        problems = [
            name for name, ok in (
                ("client_id", q.get("client_id") == client_id),
                ("response_type", q.get("response_type") == "code"),
                ("scope", q.get("scope") == "openid email"),
                ("code_challenge_method", q.get("code_challenge_method") == "S256"),
                ("code_challenge", bool(q.get("code_challenge"))),
                ("state", bool(q.get("state"))),
                ("nonce", bool(q.get("nonce"))),
                ("redirect_uri", (q.get("redirect_uri") or "").endswith("/auth/google/callback")),
            ) if not ok
        ]
        if problems:
            return HTMLResponse(f"<h1>Bad sign-in request</h1><p>{html.escape(', '.join(problems))}</p>", status_code=400)
        hidden = "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(q.get(name) or "", quote=True)}">'
            for name in ("state", "nonce", "redirect_uri", "code_challenge")
        )
        hint = html.escape(q.get("login_hint") or "", quote=True)
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Choose an account (stand-in Google)</title></head>
<body>
<h1>Choose an account</h1>
<p>to continue to Starling. This is the e2e harness's stand-in for Google.</p>
<form method="post" action="/o/oauth2/v2/auth">
{hidden}
<label for="email">Google account email</label>
<input id="email" name="email" type="email" value="{hint}" data-testid="google-email">
<label><input type="checkbox" name="email_verified" value="true" checked data-testid="google-verified"> Google has confirmed this address</label>
<label for="hd">Workspace domain (optional)</label>
<input id="hd" name="hd" type="text" value="" data-testid="google-hd">
<button type="submit" name="choice" value="continue" data-testid="google-continue">Continue</button>
<button type="submit" name="choice" value="cancel" data-testid="google-cancel">Cancel</button>
</form>
</body></html>"""
        return HTMLResponse(page)

    @app.post("/o/oauth2/v2/auth")
    async def choose(request: Request) -> RedirectResponse:
        form = await request.form()
        redirect_uri = str(form.get("redirect_uri") or "")
        state = str(form.get("state") or "")
        if form.get("choice") == "cancel":
            return RedirectResponse(redirect_uri + "?" + urlencode({"error": "access_denied", "state": state}), status_code=303)
        email = str(form.get("email") or "").strip()
        code = "e2e-code-" + secrets.token_urlsafe(18)
        codes[code] = {
            "email": email,
            "email_verified": form.get("email_verified") == "true",
            "hd": str(form.get("hd") or "").strip() or None,
            "nonce": str(form.get("nonce") or ""),
            "challenge": str(form.get("code_challenge") or ""),
            "redirect_uri": redirect_uri,
            "issued": time.time(),
        }
        return RedirectResponse(redirect_uri + "?" + urlencode({"code": code, "state": state}), status_code=303)

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        entry = codes.pop(str(form.get("code") or ""), None)
        if entry is None or time.time() - entry["issued"] > 600:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("client_id") != client_id or form.get("client_secret") != client_secret:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("redirect_uri") != entry["redirect_uri"] or form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        verifier = str(form.get("code_verifier") or "")
        if _b64url(hashlib.sha256(verifier.encode()).digest()) != entry["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier"}, status_code=400)
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": base_url, "aud": client_id, "azp": client_id, "sub": subject_for(entry["email"]),
            "email": entry["email"], "email_verified": entry["email_verified"], "iat": now, "exp": now + 3600,
            "nonce": entry["nonce"],
        }
        if entry["hd"]:
            claims["hd"] = entry["hd"]
        return JSONResponse({"access_token": "e2e-access", "id_token": sign(claims), "token_type": "Bearer",
                             "expires_in": 3599, "scope": "openid email"})

    @app.get("/oauth2/v3/certs")
    async def certs() -> JSONResponse:
        numbers = key.public_key().public_numbers()
        return JSONResponse({"keys": [{"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
                                       "n": _int_b64(numbers.n), "e": _int_b64(numbers.e)}]})

    return app
