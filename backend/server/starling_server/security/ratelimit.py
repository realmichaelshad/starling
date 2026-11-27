"""Fixed-window rate limits stored in the control database table `rate_limits`.

A limit is a `Rule(bucket, limit, window_s)`; a key identifies who is counted:

- `ip_key(prefix)` for the client's network prefix (/24 for IPv4, /64 for IPv6). The client
  address comes from `X-Forwarded-For` only when the TCP peer is listed in
  `STARLING_TRUSTED_PROXIES`; otherwise the peer address is used and the header is ignored.
- `email_key(email_norm)` for an email address (stored as a keyed hash, never the address).
- `account_key(account_id)` for an account.

Login uses progressive delays instead of lockouts: after `LOGIN_FREE_FAILURES` failures for an
email within the fixed failure window, each further attempt must wait `login_delay_s(failures)`
seconds after the previous attempt (2 s, 4 s, 8 s ... capped at 5 minutes). The attempt slot is
claimed inside the writer transaction, so parallel guesses cannot share one slot. The owner of
the address can always sign in again after at most the capped delay.
"""
from __future__ import annotations

import ipaddress
import random
import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Request

from ..config import Settings
from ..db import Database
from . import clock
from .crypto import sha256_hex


@dataclass(frozen=True)
class Rule:
    bucket: str
    limit: int
    window_s: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    count: int
    limit: int
    retry_after_s: int


SIGNUP_PER_IP = Rule("signup_ip", 10, 3600)
SIGNUP_GLOBAL_BUCKET = "signup_all"  # the limit comes from SIGNUP_GLOBAL_PER_HOUR, so the Rule is built per call
SIGNUP_GLOBAL_KEY = "all"
LOGIN_PER_IP = Rule("login_ip", 60, 900)
LOGIN_BURST_PER_EMAIL = Rule("login_burst_email", 10, 60)
FORGOT_PER_IP = Rule("forgot_ip", 20, 3600)
FORGOT_PER_EMAIL = Rule("forgot_email", 3, 3600)
TOKEN_FORM_PER_IP = Rule("email_token_ip", 30, 600)
VERIFY_RESEND_PER_ACCOUNT = Rule("verify_resend", 5, 86400)
VERIFY_RESEND_SPACING = Rule("verify_resend_spacing", 1, 60)
SIGNUP_NOTICE_PER_ACCOUNT = Rule("signup_notice", 1, 3600)
EMAIL_CHANGE_PER_ACCOUNT = Rule("email_change", 5, 86400)
PASSWORD_CHECK_PER_ACCOUNT = Rule("password_check", 10, 900)
# Requests that reach the customer's engine without using a draft (the posting-time helper and the
# customer's own saved material). Generous for a person, finite for a script.
ENGINE_UNMETERED_PER_ACCOUNT = Rule("engine_unmetered", 60, 60)
DEVICE_TOKEN_PER_IP = Rule("device_token_ip", 30, 60)
CONNECT_PER_IP = Rule("connect_ip", 60, 600)
TOTP_PER_ACCOUNT = Rule("totp_account", 5, 300)
EXPORT_PER_ACCOUNT = Rule("export", 3, 3600)
# Returns from Google: each one costs a call to Google's token endpoint. A person needs a handful.
GOOGLE_CALLBACK_PER_IP = Rule("google_callback_ip", 30, 600)
# Reads of Stripe when a customer lands on Billing back from Stripe's pages. A GET, so a page on
# another site can load it again and again in a signed-in browser; past this, the page still
# renders and the webhook still brings the change.
BILLING_REFRESH_PER_ACCOUNT = Rule("billing_refresh", 6, 600)

LOGIN_FAILURE_BUCKET = "login_fail"
LOGIN_LAST_BUCKET = "login_last"  # one row per email key; window_start = time of the last attempt
LOGIN_FAILURE_WINDOW_S = 3600
LOGIN_FREE_FAILURES = 5
LOGIN_MAX_DELAY_S = 300

CLEANUP_AGE_S = 2 * 86400
CLEANUP_PROBABILITY = 0.01


# --------------------------------------------------------------------------- keys


def email_key(email_norm: str) -> str:
    return "e:" + sha256_hex("starling-rate:" + email_norm)[:40]


def ip_key(prefix: str) -> str:
    return "ip:" + prefix


def account_key(account_id: str) -> str:
    return "a:" + account_id


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


@lru_cache(maxsize=16)
def _trusted_networks(proxies: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for item in proxies:
        try:
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_trusted(ip, networks) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


def client_ip(request: Request, settings: Settings) -> str:
    """The client address; `X-Forwarded-For` counts only behind a trusted proxy."""
    peer_host = request.client.host if request.client else ""
    peer = _parse_ip(peer_host) if peer_host else None
    networks = _trusted_networks(settings.trusted_proxies)
    if peer is not None and networks and _is_trusted(peer, networks):
        hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
        for hop in reversed(hops):
            ip = _parse_ip(hop)
            if ip is None:
                break
            if _is_trusted(ip, networks):
                continue
            return str(ip)
    if peer is not None:
        return str(peer)
    return peer_host[:64] or "unknown"


def ip_prefix(ip: str) -> str:
    parsed = _parse_ip(ip)
    if parsed is None:
        return ip[:64]
    bits = 24 if parsed.version == 4 else 64
    return str(ipaddress.ip_network(f"{parsed}/{bits}", strict=False))


def request_ip_prefix(request: Request, settings: Settings) -> str:
    return ip_prefix(client_ip(request, settings))


# --------------------------------------------------------------------------- fixed windows


def window_start(now: int, window_s: int) -> int:
    return now - now % window_s


def hit(conn: sqlite3.Connection, rule: Rule, key: str, now: int) -> Decision:
    """Count one event in the current window (inside a writer transaction)."""
    start = window_start(now, rule.window_s)
    count = conn.execute(
        "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT (bucket, key, window_start) DO UPDATE SET count = count + 1 RETURNING count",
        (rule.bucket, key, start),
    ).fetchone()[0]
    if random.random() < CLEANUP_PROBABILITY:
        conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (now - CLEANUP_AGE_S,))
    if count <= rule.limit:
        return Decision(True, int(count), rule.limit, 0)
    return Decision(False, int(count), rule.limit, max(1, start + rule.window_s - now))


def hit_all(conn: sqlite3.Connection, checks: list[tuple[Rule, str]], now: int) -> Decision:
    """Count every check; return the refusal with the longest wait, or an allowed decision."""
    refused = [d for d in (hit(conn, rule, key, now) for rule, key in checks) if not d.allowed]
    if refused:
        return max(refused, key=lambda d: d.retry_after_s)
    return Decision(True, 0, 0, 0)


# A caller who is over a limit stays over it until the window ends, and saying so again needs no
# database. Without this, every refused request still cost a synced write on the single writer, so the
# rate limiter was itself the cheapest way to flood it (`POST /api/v1/devices/token` needs no account).
# The gateway is one process with one worker, so process memory is the whole truth here; after a
# restart the database row still holds the count, and the first refusal is remembered again.
_REFUSED_MAX = 20_000
_refused: dict[tuple[int, str, str, int], int] = {}  # (database, bucket, key, window_start) -> window_end
_refused_lock = threading.Lock()


def _known_refusal(db: Database, checks: list[tuple[Rule, str]], at: int) -> Decision | None:
    waits = []
    with _refused_lock:
        for rule, key in checks:
            ends_at = _refused.get((id(db), rule.bucket, key, window_start(at, rule.window_s)))
            if ends_at is not None and ends_at > at:
                waits.append(Decision(False, rule.limit + 1, rule.limit, max(1, ends_at - at)))
    return max(waits, key=lambda d: d.retry_after_s) if waits else None


def _remember_refusals(db: Database, checks: list[tuple[Rule, str]], decisions: list[Decision], at: int) -> None:
    with _refused_lock:
        if len(_refused) >= _REFUSED_MAX:
            for stale in [k for k, ends_at in _refused.items() if ends_at <= at]:
                del _refused[stale]
            if len(_refused) >= _REFUSED_MAX:
                _refused.clear()  # never grows without bound; the database still has every count
        for (rule, key), decision in zip(checks, decisions):
            if not decision.allowed:
                start = window_start(at, rule.window_s)
                _refused[(id(db), rule.bucket, key, start)] = start + rule.window_s


def forget_refusals() -> None:
    """For tests that move the clock or reuse a database: drop what this process remembers."""
    with _refused_lock:
        _refused.clear()


async def check(db: Database, checks: list[tuple[Rule, str]], now: int | None = None) -> Decision:
    at = clock.now() if now is None else now
    known = _known_refusal(db, checks, at)
    if known is not None:
        return known
    decisions = await db.write_async(lambda conn: [hit(conn, rule, key, at) for rule, key in checks])
    _remember_refusals(db, checks, decisions, at)
    refused = [decision for decision in decisions if not decision.allowed]
    if refused:
        return max(refused, key=lambda d: d.retry_after_s)
    return Decision(True, 0, 0, 0)


def peek(conn: sqlite3.Connection, rule: Rule, key: str, now: int) -> int:
    row = conn.execute(
        "SELECT count FROM rate_limits WHERE bucket = ? AND key = ? AND window_start = ?",
        (rule.bucket, key, window_start(now, rule.window_s)),
    ).fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- login delays


def login_delay_s(failures: int) -> int:
    if failures < LOGIN_FREE_FAILURES:
        return 0
    return min(2 ** (failures - LOGIN_FREE_FAILURES + 1), LOGIN_MAX_DELAY_S)


def _login_failures(conn: sqlite3.Connection, key: str, now: int) -> int:
    row = conn.execute(
        "SELECT count FROM rate_limits WHERE bucket = ? AND key = ? AND window_start = ?",
        (LOGIN_FAILURE_BUCKET, key, window_start(now, LOGIN_FAILURE_WINDOW_S)),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_last_attempt(conn: sqlite3.Connection, key: str, now: int) -> None:
    conn.execute("DELETE FROM rate_limits WHERE bucket = ? AND key = ?", (LOGIN_LAST_BUCKET, key))
    conn.execute(
        "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES (?, ?, ?, 1)",
        (LOGIN_LAST_BUCKET, key, now),
    )


def login_gate(conn: sqlite3.Connection, key: str, now: int) -> Decision:
    """Allow or delay a login attempt for an email key; claims the attempt slot when delayed."""
    failures = _login_failures(conn, key, now)
    delay = login_delay_s(failures)
    if delay == 0:
        return Decision(True, failures, LOGIN_FREE_FAILURES, 0)
    row = conn.execute(
        "SELECT MAX(window_start) FROM rate_limits WHERE bucket = ? AND key = ?", (LOGIN_LAST_BUCKET, key)
    ).fetchone()
    last = row[0] if row else None
    if last is not None and now - int(last) < delay:
        return Decision(False, failures, LOGIN_FREE_FAILURES, max(1, delay - (now - int(last))))
    _set_last_attempt(conn, key, now)
    return Decision(True, failures, LOGIN_FREE_FAILURES, 0)


def record_login_failure(conn: sqlite3.Connection, key: str, now: int) -> None:
    conn.execute(
        "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT (bucket, key, window_start) DO UPDATE SET count = count + 1",
        (LOGIN_FAILURE_BUCKET, key, window_start(now, LOGIN_FAILURE_WINDOW_S)),
    )
    _set_last_attempt(conn, key, now)


def record_login_success(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "DELETE FROM rate_limits WHERE bucket IN (?, ?) AND key = ?", (LOGIN_FAILURE_BUCKET, LOGIN_LAST_BUCKET, key)
    )


# --------------------------------------------------------------------------- one-time markers


def claim_once(conn: sqlite3.Connection, bucket: str, key: str, marker_time: int) -> bool:
    """Record a one-time marker (for example a used TOTP step); False if it already exists."""
    cursor = conn.execute(
        "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT (bucket, key, window_start) DO NOTHING",
        (bucket, key, marker_time),
    )
    return cursor.rowcount == 1
