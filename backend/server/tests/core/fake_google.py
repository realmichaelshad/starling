"""A stand-in for Google's OpenID Connect endpoints, for route tests.

It plays both halves of Google: the account chooser (`authorize` turns the URL the gateway sent the
browser to into the code Google would hand back) and the two HTTP endpoints the gateway calls (the
token endpoint and the signing keys), installed over `auth.google.post_token` and `get_json`.
Tokens are real RS256 JWTs signed with a key made per test, so the gateway's whole verification
path runs: signature, issuer, audience, nonce, expiry and PKCE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from starling_server.security import clock

CLIENT_ID = "starling-test.apps.googleusercontent.com"
CLIENT_SECRET = "test-google-client-secret-value"
ISSUER = "https://accounts.google.com"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _int_b64(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class FakeGoogle:
    def __init__(self, redirect_uri: str) -> None:
        self.redirect_uri = redirect_uri
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-" + secrets.token_hex(4)
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_calls: list[dict[str, str]] = []
        self.jwks_calls = 0
        self.token_status: int | None = None  # force the token endpoint to fail with this status
        self.tamper: Any = None  # a function (claims) -> claims, applied before signing

    # ---- the account chooser

    def authorize(self, location: str, *, sub: str, email: str, email_verified: Any = True, hd: str | None = None,
                  **extra: Any) -> tuple[str, str]:
        """Pick an account on "Google's" page: (state, code) for the callback."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth", location
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert params["client_id"] == CLIENT_ID
        assert params["redirect_uri"] == self.redirect_uri
        assert params["response_type"] == "code"
        assert params["scope"] == "openid email"
        assert params["code_challenge_method"] == "S256"
        assert params["prompt"] == "select_account"
        code = "code-" + secrets.token_urlsafe(16)
        claims: dict[str, Any] = {"sub": sub, "email": email, "email_verified": email_verified, **extra}
        if hd:
            claims["hd"] = hd
        self.codes[code] = {"claims": claims, "nonce": params["nonce"], "challenge": params["code_challenge"],
                            "login_hint": params.get("login_hint")}
        return params["state"], code

    # ---- tokens

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                          "n": _int_b64(numbers.n), "e": _int_b64(numbers.e)}]}

    def sign(self, claims: dict[str, Any], *, key: Any = None, kid: str | None = None, alg: str = "RS256") -> str:
        header = b64url(json.dumps({"alg": alg, "kid": kid or self.kid, "typ": "JWT"}).encode())
        payload = b64url(json.dumps(claims).encode())
        signing_input = f"{header}.{payload}".encode()
        signature = (key or self.key).sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{b64url(signature)}"

    def claims_for(self, entry: dict[str, Any]) -> dict[str, Any]:
        now = clock.now()
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "azp": CLIENT_ID, "iat": now, "exp": now + 3600,
                  "nonce": entry["nonce"], **entry["claims"]}
        return self.tamper(claims) if self.tamper else claims

    # ---- the two endpoints the gateway calls

    async def post_token(self, url: str, data: dict[str, str]) -> httpx.Response:
        assert url == "https://oauth2.googleapis.com/token"
        self.token_calls.append(dict(data))
        if self.token_status is not None:
            return httpx.Response(self.token_status, json={"error": "server_error"})
        entry = self.codes.pop(data.get("code", ""), None)
        if entry is None or data.get("client_secret") != CLIENT_SECRET or data.get("client_id") != CLIENT_ID:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if data.get("redirect_uri") != self.redirect_uri or data.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = data.get("code_verifier", "")
        if b64url(hashlib.sha256(verifier.encode()).digest()) != entry["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
        token = self.sign(self.claims_for(entry))
        return httpx.Response(200, json={"access_token": "ya29.test", "id_token": token, "token_type": "Bearer",
                                         "expires_in": 3599, "scope": "openid email"})

    async def get_json(self, url: str) -> httpx.Response:
        assert url == "https://www.googleapis.com/oauth2/v3/certs"
        self.jwks_calls += 1
        return httpx.Response(200, json=self.jwks())
