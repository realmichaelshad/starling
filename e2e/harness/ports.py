"""Free loopback ports, chosen at runtime. Nothing in the harness ever binds a fixed port."""
from __future__ import annotations

import socket
import sys


def free_port() -> int:
    """A port no listener holds right now.

    The socket is closed before the port is handed out, so there is a small window in
    which something else could take it. On Windows the probe socket asks for exclusive
    use, which stops another probe in the same run from returning the same number.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0x7FFFFFFB), 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0
