"""Shared fixtures: isolated settings, a migrated control database and a test client."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.config import Settings, load_settings
from starling_server.db import Database

TEST_SECRET_KEY = base64.urlsafe_b64encode(b"starling-test-secret-key-32bytes!").decode().rstrip("=")
TEST_EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
PUBLIC_URL = "http://127.0.0.1:8765"


def base_env(data_dir: Path) -> dict[str, str]:
    return {
        "STARLING_ENV": "development",
        "STARLING_PUBLIC_URL": PUBLIC_URL,
        "STARLING_DATA_DIR": str(data_dir),
        "STARLING_ISOLATION": "none",
        "STARLING_SECRET_KEY": TEST_SECRET_KEY,
        "STARLING_EXTENSION_IDS": TEST_EXTENSION_ID,
        "STARLING_OWNER_EMAILS": "owner@example.com",
        "EMAIL_MODE": "outbox",
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_KIB": "8192",
        "ARGON2_PARALLELISM": "1",
    }


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return base_env(tmp_path / "data")


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env)


@pytest.fixture
def database(settings: Settings):
    db = Database(settings.control_db_path)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=PUBLIC_URL) as test_client:
        yield test_client
