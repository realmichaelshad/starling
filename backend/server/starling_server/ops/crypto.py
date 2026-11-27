"""Encrypted container for backup archives: scrypt key derivation and chunked AES-256-GCM.

Layout (integers big-endian):

    header (72 bytes)
        magic        8s   b"STLBACK1"
        version      B    1
        kdf          B    1 = scrypt
        log2_n       B    scrypt cost, N = 2 ** log2_n
        r, p         B B  scrypt block size and parallelism
        salt         16s  random per archive
        nonce_prefix 7s   random per archive
        chunk_size   I    plaintext bytes per chunk (only the final chunk may be shorter)
        header_mac   32s  HMAC-SHA256(mac_key, the 40 bytes above)
    chunks
        AES-256-GCM(key, nonce_prefix || counter (4 bytes) || final flag (1 byte),
                    plaintext, associated data = header) = ciphertext || 16-byte tag

scrypt(passphrase, salt) gives 64 bytes: the AES key and the header MAC key, so a wrong
passphrase is reported before any data is decrypted. Each chunk is authenticated and bound
to its position, to the header and (through the final flag) to the end of the stream, so bit
flips, reordering, truncation and appended data are all detected. Memory use stays at about
two chunks whatever the archive size.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import struct
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .common import BackupConfigError, BackupError, BackupIntegrityError, WrongPassphraseError

MAGIC = b"STLBACK1"
FORMAT_VERSION = 1
KDF_SCRYPT = 1
DEFAULT_LOG2_N = 17  # with r = 8: about 128 MiB of memory for a fraction of a second per archive
DEFAULT_R = 8
DEFAULT_P = 1
DEFAULT_CHUNK_SIZE = 1 << 20
TAG_SIZE = 16
SALT_SIZE = 16
NONCE_PREFIX_SIZE = 7
MAX_KDF_MEMORY = 512 << 20
MIN_CHUNK_SIZE = 1 << 10
MAX_CHUNK_SIZE = 64 << 20
MAX_CHUNKS = 0xFFFFFFFF

_FIELDS = struct.Struct(">8sBBBBB16s7sI")
HEADER_SIZE = _FIELDS.size + 32


def _check_parameters(log2_n: int, r: int, p: int, chunk_size: int) -> None:
    if not (10 <= log2_n <= 24 and 1 <= r <= 32 and 1 <= p <= 16) or 128 * r * (1 << log2_n) > MAX_KDF_MEMORY:
        raise BackupIntegrityError("unsupported key derivation parameters in the archive header")
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise BackupIntegrityError("unsupported chunk size in the archive header")


def derive_keys(passphrase: str, salt: bytes, log2_n: int, r: int, p: int) -> tuple[bytes, bytes]:
    """Return (aes_key, header_mac_key) for this passphrase and salt."""
    secret = passphrase.encode("utf-8")
    if not secret:
        raise BackupConfigError("the backup passphrase is empty")
    material = Scrypt(salt=salt, length=64, n=1 << log2_n, r=r, p=p).derive(secret)
    return material[:32], material[32:]


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    return prefix + struct.pack(">IB", counter, 1 if final else 0)


class EncryptingWriter(io.RawIOBase):
    """Write-only stream that encrypts into `raw`. Call `finish()` exactly once at the end."""

    def __init__(
        self,
        raw: BinaryIO,
        passphrase: str,
        *,
        log2_n: int = DEFAULT_LOG2_N,
        r: int = DEFAULT_R,
        p: int = DEFAULT_P,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        super().__init__()
        _check_parameters(log2_n, r, p, chunk_size)
        salt = os.urandom(SALT_SIZE)
        prefix = os.urandom(NONCE_PREFIX_SIZE)
        key, mac_key = derive_keys(passphrase, salt, log2_n, r, p)
        fields = _FIELDS.pack(MAGIC, FORMAT_VERSION, KDF_SCRYPT, log2_n, r, p, salt, prefix, chunk_size)
        self._header = fields + hmac.new(mac_key, fields, hashlib.sha256).digest()
        self._aead = AESGCM(key)
        self._prefix = prefix
        self._raw = raw
        self._chunk_size = chunk_size
        self._buffer = bytearray()
        self._counter = 0
        self._finished = False
        raw.write(self._header)

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # type: ignore[override]
        if self._finished:
            raise ValueError("the archive stream is already finished")
        view = memoryview(data)
        self._buffer += view
        # Keep at least one byte buffered so the final chunk is always written by finish().
        while len(self._buffer) > self._chunk_size:
            self._emit(bytes(self._buffer[: self._chunk_size]), final=False)
            del self._buffer[: self._chunk_size]
        return view.nbytes

    def _emit(self, chunk: bytes, final: bool) -> None:
        if self._counter >= MAX_CHUNKS:
            raise BackupError("archive is too large for the container format")
        nonce = _nonce(self._prefix, self._counter, final)
        self._raw.write(self._aead.encrypt(nonce, chunk, self._header))
        self._counter += 1

    def finish(self) -> None:
        if self._finished:
            return
        self._emit(bytes(self._buffer), final=True)
        self._buffer.clear()
        self._finished = True


class DecryptingReader(io.RawIOBase):
    """Read-only stream that yields authenticated plaintext chunk by chunk."""

    def __init__(self, raw: BinaryIO, passphrase: str) -> None:
        super().__init__()
        header = raw.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[:8] != MAGIC:
            raise BackupIntegrityError("not a Starling backup archive")
        fields, mac = header[: _FIELDS.size], header[_FIELDS.size :]
        _, version, kdf, log2_n, r, p, salt, prefix, chunk_size = _FIELDS.unpack(fields)
        if version != FORMAT_VERSION or kdf != KDF_SCRYPT:
            raise BackupIntegrityError(f"unsupported archive format version {version}")
        _check_parameters(log2_n, r, p, chunk_size)
        key, mac_key = derive_keys(passphrase, salt, log2_n, r, p)
        if not hmac.compare_digest(mac, hmac.new(mac_key, fields, hashlib.sha256).digest()):
            raise WrongPassphraseError("wrong backup passphrase (or the archive header was modified)")
        self._header = header
        self._aead = AESGCM(key)
        self._prefix = prefix
        self._raw = raw
        self._full = chunk_size + TAG_SIZE
        self._counter = 0
        self._pending = raw.read(self._full)
        self._out = b""
        self._pos = 0
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        while self._pos >= len(self._out):
            if self._done:
                return 0
            self._next_chunk()
        view = memoryview(buffer).cast("B")
        count = min(len(view), len(self._out) - self._pos)
        view[:count] = memoryview(self._out)[self._pos : self._pos + count]
        self._pos += count
        return count

    def _next_chunk(self) -> None:
        current = self._pending
        following = self._raw.read(self._full)
        if len(current) < TAG_SIZE or (following and len(current) < self._full):
            raise BackupIntegrityError("archive is truncated or malformed")
        final = not following
        try:
            self._out = self._aead.decrypt(_nonce(self._prefix, self._counter, final), current, self._header)
        except InvalidTag:
            raise BackupIntegrityError("archive is corrupted, truncated or was modified") from None
        self._pos = 0
        self._counter += 1
        self._pending = following
        self._done = final
