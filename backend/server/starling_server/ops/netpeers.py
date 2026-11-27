"""Established TCP peers of a local port, read from /proc/net/tcp and /proc/net/tcp6 (Linux).

Run on the first deployment while the site receives traffic
(`python -m starling_server.cli proxy-peers`) to see which addresses the platform's proxy connects
from; those, and nothing broader, belong in STARLING_TRUSTED_PROXIES.
"""
from __future__ import annotations

import ipaddress
import sys
from collections import Counter
from pathlib import Path

PROC_FILES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
TCP_ESTABLISHED = 1


def decode_address(hex_address: str, byteorder: str = sys.byteorder) -> str:
    """Kernel hex notation (32-bit words in host byte order) to text; IPv4-mapped IPv6 becomes IPv4."""
    raw = bytes.fromhex(hex_address)
    if byteorder == "little":
        raw = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    address = ipaddress.ip_address(raw)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _endpoint(token: str) -> tuple[str, int] | None:
    host, separator, port = token.partition(":")
    if not separator or len(host) not in (8, 32) or len(port) != 4:
        return None
    try:
        return host, int(port, 16)
    except ValueError:
        return None


def established_peers(port: int, tables: list[str] | None = None, byteorder: str = sys.byteorder) -> Counter:
    """Count established connections to local `port`, by remote address."""
    if tables is None:
        tables = [path.read_text() for path in PROC_FILES if path.exists()]
    counts: Counter = Counter()
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            endpoints = [(index, found) for index, found in ((i, _endpoint(f)) for i, f in enumerate(fields)) if found]
            if len(endpoints) < 2:
                continue
            (_, local), (remote_index, remote) = endpoints[0], endpoints[1]
            if local[1] != port or remote_index + 1 >= len(fields):
                continue
            try:
                if int(fields[remote_index + 1], 16) == TCP_ESTABLISHED:
                    counts[decode_address(remote[0], byteorder)] += 1
            except ValueError:
                continue
    return counts
