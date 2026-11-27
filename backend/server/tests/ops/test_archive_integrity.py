"""Archives that are wrong, damaged or crafted are rejected before anything is restored."""
from __future__ import annotations

import io
import os

import pytest

from starling_server.ops import crypto
from starling_server.ops.archive import verify_archive
from starling_server.ops.common import BackupIntegrityError, WrongPassphraseError
from starling_server.ops.restore import restore_archive

from .conftest import FAST_KDF, PASSPHRASE, edit_manifest, rewrite_archive


def test_wrong_passphrase_fails_and_restores_nothing(small_archive, ops_settings, tmp_path):
    with pytest.raises(WrongPassphraseError):
        verify_archive(small_archive.path, "not the right passphrase")
    target = tmp_path / "fresh"
    with pytest.raises(WrongPassphraseError):
        restore_archive(ops_settings, small_archive.path, target, passphrase="not the right passphrase")
    assert not target.exists()


@pytest.mark.parametrize(
    "damage",
    ["flip-body", "flip-last-byte", "truncate", "drop-final-chunk", "append", "swap-chunks"],
)
def test_tampered_ciphertext_fails(small_archive, damage):
    data = bytearray(small_archive.path.read_bytes())
    body = crypto.HEADER_SIZE
    full = crypto.DEFAULT_CHUNK_SIZE + crypto.TAG_SIZE
    assert len(data) > body + 2 * full, "fixture must span several chunks"
    if damage == "flip-body":
        data[body + full + 100] ^= 0x01
    elif damage == "flip-last-byte":
        data[-1] ^= 0x80
    elif damage == "truncate":
        del data[-10:]
    elif damage == "drop-final-chunk":
        chunks = (len(data) - body - 1) // full
        del data[body + chunks * full:]
    elif damage == "append":
        data += os.urandom(64)
    elif damage == "swap-chunks":
        first, second = data[body:body + full], data[body + full:body + 2 * full]
        data[body:body + 2 * full] = second + first
    small_archive.path.write_bytes(bytes(data))
    with pytest.raises(BackupIntegrityError) as info:
        verify_archive(small_archive.path, PASSPHRASE)
    assert not isinstance(info.value, WrongPassphraseError)


def test_modified_header_is_detected(small_archive):
    data = bytearray(small_archive.path.read_bytes())
    data[20] ^= 0x01  # inside the salt
    small_archive.path.write_bytes(bytes(data))
    with pytest.raises(WrongPassphraseError):
        verify_archive(small_archive.path, PASSPHRASE)


def test_not_an_archive_is_rejected(tmp_path):
    path = tmp_path / "random.sbk"
    path.write_bytes(os.urandom(4096))
    with pytest.raises(BackupIntegrityError, match="not a Starling backup archive"):
        verify_archive(path, PASSPHRASE)


def test_manifest_checksum_mismatch_fails(small_archive):
    def wrong_checksum(manifest):
        entry = next(item for item in manifest["files"] if item["path"] == "tenants/2/notes.txt")
        entry["sha256"] = "0" * 64

    rewrite_archive(small_archive.path, lambda members: edit_manifest(members, wrong_checksum))
    with pytest.raises(BackupIntegrityError, match="checksum mismatch"):
        verify_archive(small_archive.path, PASSPHRASE)


def test_file_content_changed_behind_the_manifest_fails(small_archive):
    def change(members):
        return [(m, b"replaced" if m.name == "tenants/1/starling.json" else d) for m, d in members]

    rewrite_archive(small_archive.path, change)
    with pytest.raises(BackupIntegrityError, match="checksum mismatch"):
        verify_archive(small_archive.path, PASSPHRASE)


def test_missing_file_fails(small_archive):
    rewrite_archive(small_archive.path, lambda members: [(m, d) for m, d in members if m.name != "tenants/2/notes.txt"])
    with pytest.raises(BackupIntegrityError, match="1 missing"):
        verify_archive(small_archive.path, PASSPHRASE)


@pytest.mark.parametrize("name", ["tenants/1/../../escape.txt", "/etc/cron.d/job", "control/other.db", "tenants/x/a"])
def test_unsafe_member_names_are_rejected(small_archive, ops_settings, tmp_path, name):
    def rename(members):
        for member, _ in members:
            if member.name == "tenants/2/notes.txt":
                member.name = name
        return members

    rewrite_archive(small_archive.path, rename)
    with pytest.raises(BackupIntegrityError, match="unsafe or unexpected name"):
        restore_archive(ops_settings, small_archive.path, tmp_path / "fresh")
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "fresh").exists()


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1025, 4096, 5000])
def test_crypto_stream_round_trip(size):
    payload = os.urandom(size)
    raw = io.BytesIO()
    writer = crypto.EncryptingWriter(raw, PASSPHRASE, log2_n=FAST_KDF, chunk_size=1024)
    for start in range(0, size, 700):
        writer.write(payload[start:start + 700])
    writer.finish()
    raw.seek(0)
    assert crypto.DecryptingReader(raw, PASSPHRASE).read() == payload
