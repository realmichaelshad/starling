"""Fixtures and helpers for the operations tests (backups, restore, retention, scheduler)."""
from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from starling_server.config import Settings, load_settings
from starling_server.db import Database
from starling_server.ops import crypto
from starling_server.ops.archive import BackupResult, create_backup

from ..conftest import base_env

PASSPHRASE = "correct horse battery staple, test only"
FAST_KDF = 12  # the real default (2**17) is exercised by the scheduler and command tests
FIXED_NOW = datetime(2026, 9, 14, 3, 0, 0, tzinfo=timezone.utc)

Members = list[tuple[tarfile.TarInfo, bytes]]


@pytest.fixture
def ops_env(tmp_path: Path) -> dict[str, str]:
    env = base_env(tmp_path / "data")
    env["BACKUP_DIR"] = str(tmp_path / "archives")
    env["BACKUP_PASSPHRASE"] = PASSPHRASE
    return env


@pytest.fixture
def ops_settings(ops_env: dict[str, str]) -> Settings:
    return load_settings(ops_env)


def populate_control(settings: Settings, accounts: int = 3) -> None:
    now = 1_757_800_000
    database = Database(settings.control_db_path)
    try:
        def insert(conn: sqlite3.Connection) -> None:
            for seq in range(1, accounts + 1):
                conn.execute(
                    "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, password_hash,"
                    " password_changed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.UUID(int=seq)), seq, f"user{seq}@example.com", f"user{seq}@example.com",
                     f"allow-{seq}", "argon2-placeholder", now, now),
                )
            conn.execute(
                "INSERT INTO service_state (key, value_json, updated_at) VALUES ('owner_pause', ?, ?)",
                (json.dumps({"paused": False}), now),
            )

        database.write(insert)
    finally:
        database.close()


def write_tenant_files(settings: Settings) -> dict[str, bytes]:
    """Plain files for tenants 1 and 2; returns their bytes by archive path."""
    files = {
        "tenants/1/starling.json": json.dumps({"voice": "calm", "n": 1}).encode(),
        "tenants/1/cache/blob.bin": os.urandom(2_500_000),  # incompressible: several encrypted chunks
        "tenants/2/notes.txt": "second tenant\n".encode(),
    }
    for rel, data in files.items():
        path = settings.data_dir.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (settings.tenants_dir / "2" / "empty-folder").mkdir(parents=True, exist_ok=True)
    return files


def make_tenant_db(path: Path, rows: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        conn.executemany("INSERT INTO posts (body) VALUES (?)", [(f"post {i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture
def small_archive(ops_settings: Settings) -> BackupResult:
    populate_control(ops_settings)
    write_tenant_files(ops_settings)
    make_tenant_db(ops_settings.tenants_dir / "1" / "starling.db")
    return create_backup(ops_settings, kdf_log2_n=FAST_KDF, now=FIXED_NOW)


def decrypt_members(path: Path, passphrase: str = PASSPHRASE) -> Members:
    with open(path, "rb") as raw:
        compressed = crypto.DecryptingReader(raw, passphrase).read()
    members: Members = []
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(compressed)), mode="r:") as tar:
        for member in tar.getmembers():
            data = tar.extractfile(member).read() if member.isfile() else b""
            members.append((member, data))
    return members


def rewrite_archive(path: Path, edit: Callable[[Members], Members], passphrase: str = PASSPHRASE) -> None:
    """Decrypt, let `edit` change the tar members, then re-encrypt validly with the same passphrase."""
    members = edit(decrypt_members(path, passphrase))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.PAX_FORMAT) as tar:
        for member, data in members:
            if member.isfile():
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            else:
                tar.addfile(member)
    with open(path, "wb") as raw:
        writer = crypto.EncryptingWriter(raw, passphrase, log2_n=FAST_KDF)
        writer.write(gzip.compress(buffer.getvalue()))
        writer.finish()


def edit_manifest(members: Members, change: Callable[[dict], None]) -> Members:
    out: Members = []
    for member, data in members:
        if member.name == "starling-manifest.json":
            manifest = json.loads(data)
            change(manifest)
            data = json.dumps(manifest).encode()
        out.append((member, data))
    return out
