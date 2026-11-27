"""The engine's bearer token: where it lives, and who can get it.

A hosted engine (CONTRACTS §3.1) receives its worker token on stdin and keeps it
in memory only: it is never written to disk and never placed in the environment.
Development and test runs keep a token in its own file instead.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from app import security

WORKER_TOKEN = "w" * 43


def test_the_token_is_stable_and_persisted() -> None:
    first = security.ensure_token()
    security._token_cache = None                 # what a restart sees
    second = security.ensure_token()

    assert first == second
    assert security.token_is_valid(first)
    assert security.TOKEN_PATH.read_text(encoding="utf-8").strip() == first


# RETIRED (API-native pass, 2026-09-22): test_the_token_is_not_kept_in_the_operational_config: the desktop config file (starling.json) is gone; the token lives in memory (hosted) or its own file.


# RETIRED (API-native pass, 2026-09-22): test_a_corrupt_config_does_not_rotate_the_token: the desktop config file (starling.json) is gone; the token lives in memory (hosted) or its own file.


def test_concurrent_reads_never_rotate_the_token() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: security.ensure_token(), range(24)))

    assert len(set(tokens)) == 1


def test_a_non_ascii_credential_is_rejected_not_a_crash() -> None:
    security.ensure_token()
    for supplied in ("é", "日本語", "‮txet"):
        assert security.token_is_valid(supplied) is False


def test_the_token_file_is_owner_only() -> None:
    security.ensure_token()
    if sys.platform == "win32":
        return          # mode bits are not the mechanism on Windows
    assert security.TOKEN_PATH.stat().st_mode & 0o777 == 0o600


def test_a_hosted_engine_keeps_the_worker_token_in_memory(monkeypatch) -> None:
    monkeypatch.setenv("STARLING_HOSTED", "1")
    security.set_worker_token(WORKER_TOKEN)

    assert security.ensure_token() == WORKER_TOKEN
    assert security.token_is_valid(WORKER_TOKEN)
    assert not security.token_is_valid("w" * 42)
    assert not security.TOKEN_PATH.exists(), "a hosted engine must never write a token file"
    if security.APP_DIR.exists():
        for path in security.APP_DIR.rglob("*"):
            if path.is_file():
                assert WORKER_TOKEN.encode() not in path.read_bytes()


def test_a_hosted_engine_without_a_worker_token_denies_everything(monkeypatch) -> None:
    monkeypatch.setenv("STARLING_HOSTED", "1")
    security.set_worker_token(None)

    assert security.ensure_token() == ""
    assert security.token_is_valid("anything") is False
    assert security.token_is_valid("") is False
    assert not security.TOKEN_PATH.exists()
