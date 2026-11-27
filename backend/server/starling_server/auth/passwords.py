"""Password hashing (argon2id) and the password policy.

- Parameters come from `ARGON2_TIME_COST`, `ARGON2_MEMORY_KIB` and `ARGON2_PARALLELISM`.
- At most `ARGON2_MAX_CONCURRENT` hashes or verifications run at once (memory-hard work must
  not exhaust the machine under a burst of logins). Async callers run the work in a thread.
- When an account does not exist, `verify_password(settings, None, password)` verifies against
  a dummy hash with the same parameters, so the response time does not reveal the account.
- Policy: 10 to 128 characters, not a common password, not the email address.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from functools import lru_cache

import anyio
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import Settings

MIN_LENGTH = 10
MAX_LENGTH = 128

# Lowercase. Only entries that satisfy the length rule matter; shorter ones fail anyway.
COMMON_PASSWORDS = frozenset(
    """
    0000000000 0123456789 0987654321 1111111111 1122334455 1212121212 1231231231 123123123123
    1234512345 1234554321 1234567890 12345678910 1234567890a 123456789a 123456789q 1234qwerasdf
    1q2w3e4r5t 1q2w3e4r5t6y 1q2w3e4r5t6y7u8i 1qaz2wsx3edc 2222222222 5555555555 7777777777
    9876543210 9999999999 a123456789 aa12345678 abc1234567 abc123abc123 abcd123456 abcdef1234
    abcdefghij admin12345 admin123456 administrator asdfghjkl1 asdfghjkl123 autumn2026
    baseball123 basketball basketball1 batman12345 changeme123 chocolate1 computer123
    december12 dragon12345 facebook123 football123 freedom123 google1234 hello12345 helloworld
    helloworld1 helloworld123 iloveu1234 iloveyou12 iloveyou123 iloveyou1234 internet123
    letmein123 letmein1234 letmeinnow loveyou123 master12345 monkey12345 myspace123
    p@ssw0rd123 p@ssword123 passw0rd123 password!1 password12 password123 password1234
    password12345 password2025 password2026 passwordpassword princess123 q1w2e3r4t5
    qazwsxedc123 qwer1234asdf qwerty1234 qwerty12345 qwerty123456 qwerty123! qwertyuiop
    qwertyuiop123 rootroot12 shadow12345 spring2026 starling123 starling2026 starwars123
    summer2025 summer2026 sunshine123 superman123 test123456 testtest12 trustno1234
    twitter123 welcome123 welcome1234 welcome2025 welcome2026 whatever123 winter2025
    winter2026 zaq12wsxcde3 zxcvbnm123
    """.split()
)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    needs_rehash: bool = False


def policy_errors(password: str, email: str | None = None) -> list[str]:
    """Human-readable reasons the password is not acceptable (empty when it is)."""
    if len(password) < MIN_LENGTH:
        return [f"Use at least {MIN_LENGTH} characters."]
    if len(password) > MAX_LENGTH:
        return [f"Use at most {MAX_LENGTH} characters."]
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) <= 2:
        return ["This password is too common. Choose one that is harder to guess."]
    if email:
        address = email.strip().lower()
        local = address.split("@", 1)[0]
        if lowered == address or (len(local) >= 4 and lowered == local):
            return ["Don't use your email address as your password."]
    return []


@lru_cache(maxsize=8)
def _hasher(time_cost: int, memory_kib: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def hasher_for(settings: Settings) -> PasswordHasher:
    return _hasher(settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism)


_semaphores: dict[int, threading.BoundedSemaphore] = {}
_semaphores_lock = threading.Lock()


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _semaphores_lock:
        semaphore = _semaphores.get(limit)
        if semaphore is None:
            semaphore = _semaphores[limit] = threading.BoundedSemaphore(limit)
        return semaphore


@lru_cache(maxsize=8)
def _dummy_hash_for(time_cost: int, memory_kib: int, parallelism: int) -> str:
    return _hasher(time_cost, memory_kib, parallelism).hash(secrets.token_urlsafe(24))


def dummy_hash(settings: Settings) -> str:
    return _dummy_hash_for(settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism)


def hash_password(settings: Settings, password: str) -> str:
    with _semaphore(settings.argon2_max_concurrent):
        return hasher_for(settings).hash(password)


def verify_password(settings: Settings, password_hash: str | None, password: str) -> VerifyResult:
    """Verify; with `password_hash=None` the dummy hash is checked and the result is False."""
    target = password_hash or dummy_hash(settings)
    hasher = hasher_for(settings)
    with _semaphore(settings.argon2_max_concurrent):
        try:
            hasher.verify(target, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return VerifyResult(False)
    if not password_hash:
        return VerifyResult(False)
    try:
        return VerifyResult(True, hasher.check_needs_rehash(password_hash))
    except InvalidHashError:
        return VerifyResult(True, True)


async def hash_password_async(settings: Settings, password: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, settings, password)


async def verify_password_async(settings: Settings, password_hash: str | None, password: str) -> VerifyResult:
    return await anyio.to_thread.run_sync(verify_password, settings, password_hash, password)
