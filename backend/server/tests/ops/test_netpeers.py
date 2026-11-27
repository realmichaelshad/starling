"""`proxy-peers` reads the kernel's TCP tables to show which addresses connect to the gateway."""
from __future__ import annotations

from pathlib import Path

from starling_server import cli
from starling_server.ops.netpeers import decode_address, established_peers

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
# Little-endian kernel notation. Local 172.17.0.2; peers 172.16.1.2 (twice, established),
# 172.16.1.3 (closing), and 172.16.1.5 on port 22 (ignored).
TCP4 = HEADER + (
    "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1000\n"
    "   1: 020011AC:1F90 020110AC:C350 01 00000000:00000000 00:00000000 00000000 10001        0 1001\n"
    "   2: 020011AC:1F90 020110AC:C351 01 00000000:00000000 00:00000000 00000000 10001        0 1002\n"
    "   3: 020011AC:1F90 030110AC:C352 08 00000000:00000000 00:00000000 00000000 10001        0 1003\n"
    "   4: 020011AC:0016 050110AC:C353 01 00000000:00000000 00:00000000 00000000     0        0 1004\n"
)
# Peers fdaa::3 and the IPv4-mapped ::ffff:172.16.1.9.
TCP6 = HEADER + (
    "   0: 00000000000000000000000000000000:1F90 0000AAFD000000000000000003000000:D431 01"
    " 00000000:00000000 00:00000000 00000000 10001 0 2001\n"
    "   1: 00000000000000000000000000000000:1F90 0000000000000000FFFF0000090110AC:D432 01"
    " 00000000:00000000 00:00000000 00000000 10001 0 2002\n"
)


def test_established_peers_are_counted_by_address():
    peers = established_peers(8080, [TCP4, TCP6], byteorder="little")
    assert dict(peers) == {"172.16.1.2": 2, "fdaa::3": 1, "172.16.1.9": 1}


def test_addresses_decode_on_both_byte_orders():
    assert decode_address("0100007F", byteorder="little") == "127.0.0.1"
    assert decode_address("AC100102", byteorder="big") == "172.16.1.2"


def test_command_needs_linux(capsys):
    code = cli.main(["proxy-peers"])
    if Path("/proc/net/tcp").exists():
        assert code == 0
    else:
        assert code == 2 and "only on Linux" in capsys.readouterr().err
