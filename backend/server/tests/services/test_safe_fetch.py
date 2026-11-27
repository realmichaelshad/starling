"""SSRF guard of the world fetcher: validated public addresses only, pinned connections, re-validated redirects."""
from __future__ import annotations

import asyncio
import gzip
import ssl

import httpcore
import pytest

from starling_server.services.world.safe_fetch import FetchError, PinnedNetworkBackend, SafeFetcher, Target, public_address

from .fakes_net import RecordingBackend, ScriptedResolver, http_response, request_line_and_host

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
PAGE = b"<html><head><title>Hello</title></head><body><p>Readable text.</p></body></html>"


def run(coro):
    return asyncio.run(coro)


def make_fetcher(resolver, backend) -> SafeFetcher:
    return SafeFetcher(resolver=resolver, network_backend=backend, ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


def page(address, port, raw):
    return http_response(200, PAGE, {"Content-Type": "text/html; charset=utf-8"})


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "224.0.0.1", "240.0.0.1", "255.255.255.255", "192.0.2.10", "198.18.0.1", "::1", "::", "fe80::1",
        "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
        "::ffff:169.254.169.254", "64:ff9b::a00:1", "2002:a00:1::1", "2001::1", "::7f00:1", "fe80::1%eth0",
        # Every 6to4 address, whatever it wraps: deprecated by RFC 7526, and CPython's own verdict on
        # it changed between patch releases, so the guard decides rather than the standard library.
        "2002:5db8:d822::1", "2002:101:101::1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_blocked(address):
    assert public_address(address) is None


@pytest.mark.parametrize(
    ("address", "normalized"),
    [(PUBLIC_V4, PUBLIC_V4), (PUBLIC_V6, PUBLIC_V6), ("::ffff:93.184.216.34", PUBLIC_V4)],
)
def test_public_addresses_are_allowed(address, normalized):
    assert public_address(address) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://10.0.0.8/admin", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
        "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "https://[fc00::1]/", "http://0.0.0.0/",
        "http://2130706433/", "http://0x7f.1/", "http://017700000001/", "http://localhost/", "http://printer.local/",
        "http://metadata.internal/",
    ],
)
def test_private_literals_and_internal_names_are_refused_without_resolving_or_connecting(url):
    resolver, backend = ScriptedResolver({}), RecordingBackend(page)
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get(url, max_bytes=1000, timeout_s=5))
    assert (caught.value.http_status, caught.value.code) == (403, "destination_blocked")
    assert backend.connections == [] and resolver.calls == []


@pytest.mark.parametrize(
    "answers", [["10.0.0.7"], ["127.0.0.1"], ["::1"], ["::ffff:192.168.0.1"], [PUBLIC_V4, "10.0.0.7"], ["fd00::1"]]
)
def test_host_names_resolving_to_any_non_public_address_are_refused(answers):
    resolver, backend = ScriptedResolver({"news.example": [answers]}), RecordingBackend(page)
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("https://news.example/story", max_bytes=1000, timeout_s=5))
    assert caught.value.code == "destination_blocked"
    assert backend.connections == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd", "ftp://news.example/", "gopher://news.example/", "http://user:pw@news.example/",
        "http://news.example:22/", "https://news.example:8443/", "javascript:alert(1)", "//news.example/", "", None,
    ],
)
def test_only_plain_http_urls_on_standard_ports_are_accepted(url):
    resolver, backend = ScriptedResolver({"news.example": [[PUBLIC_V4]]}), RecordingBackend(page)
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get(url, max_bytes=1000, timeout_s=5))
    assert (caught.value.http_status, caught.value.code) == (400, "bad_request")
    assert backend.connections == [] and resolver.calls == []


def test_the_connection_goes_to_the_validated_address_with_the_real_host_header():
    resolver, backend = ScriptedResolver({"news.example": [[PUBLIC_V4]]}), RecordingBackend(page)
    result = run(make_fetcher(resolver, backend).get("http://news.example/story?id=1", max_bytes=10_000, timeout_s=5))
    assert (result.status, result.content, result.url) == (200, PAGE, "http://news.example/story?id=1")
    assert backend.connections == [(PUBLIC_V4, 80)]
    request_line, host = request_line_and_host(backend.requests[0][1])
    assert request_line == "GET /story?id=1 HTTP/1.1" and host == "news.example"


def test_https_verifies_the_host_name_while_connecting_to_the_validated_ip():
    resolver, backend = ScriptedResolver({"secure.example": [[PUBLIC_V6]]}), RecordingBackend(page)
    run(make_fetcher(resolver, backend).get("https://secure.example/", max_bytes=10_000, timeout_s=5))
    assert backend.connections == [(PUBLIC_V6, 443)]
    assert backend.tls_server_names == ["secure.example"]


def test_dns_rebinding_cannot_move_the_connection():
    """The resolver answers public first and loopback afterwards: only the first, validated answer is used."""
    resolver = ScriptedResolver({"rebind.example": [[PUBLIC_V4], ["127.0.0.1"]]})
    backend = RecordingBackend(page)
    fetcher = make_fetcher(resolver, backend)
    result = run(fetcher.get("http://rebind.example/a", max_bytes=10_000, timeout_s=5))
    assert result.status == 200
    assert resolver.calls == ["rebind.example"]  # one lookup per hop, never a second one to connect
    assert backend.connections == [(PUBLIC_V4, 80)]
    with pytest.raises(FetchError) as caught:
        run(fetcher.get("http://rebind.example/b", max_bytes=10_000, timeout_s=5))
    assert caught.value.code == "destination_blocked"
    assert backend.connections == [(PUBLIC_V4, 80)]


def test_rebinding_during_a_redirect_is_caught_by_revalidation():
    resolver = ScriptedResolver({"rebind.example": [[PUBLIC_V4], ["127.0.0.1"]]})
    backend = RecordingBackend(lambda address, port, raw: http_response(302, b"", {"Location": "/next"}))
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("http://rebind.example/start", max_bytes=10_000, timeout_s=5))
    assert caught.value.code == "destination_blocked"
    assert resolver.calls == ["rebind.example", "rebind.example"]
    assert backend.connections == [(PUBLIC_V4, 80)]


@pytest.mark.parametrize(
    "location",
    [
        "http://internal.example/secret", "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data",
        "http://localhost/", "ftp://news.example/", "http://news.example:8080/",
    ],
)
def test_redirect_targets_are_validated_like_the_first_url(location):
    resolver = ScriptedResolver({"news.example": [[PUBLIC_V4]], "internal.example": [["10.0.0.7"]]})
    backend = RecordingBackend(lambda address, port, raw: http_response(302, b"", {"Location": location}))
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("http://news.example/start", max_bytes=10_000, timeout_s=5))
    assert caught.value.code in ("destination_blocked", "bad_request")
    assert backend.connections == [(PUBLIC_V4, 80)]


def _hops(final_hop: int):
    def responder(address, port, raw):
        request_line, _ = request_line_and_host(raw)
        hop = int(request_line.split()[1].strip("/") or 0)
        if hop < final_hop:
            return http_response(302, b"", {"Location": f"/{hop + 1}"})
        return page(address, port, raw)

    return responder


def test_at_most_three_redirects_are_followed():
    resolver = ScriptedResolver({"hop.example": [[PUBLIC_V4]]})
    backend = RecordingBackend(_hops(3))
    result = run(make_fetcher(resolver, backend).get("http://hop.example/0", max_bytes=10_000, timeout_s=5))
    assert result.status == 200 and result.url == "http://hop.example/3"
    backend = RecordingBackend(_hops(4))
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("http://hop.example/0", max_bytes=10_000, timeout_s=5))
    assert caught.value.code == "fetch_failed"
    assert len(backend.connections) == 4


def test_bodies_are_capped_truncated_for_pages_and_refused_otherwise():
    body = b"a" * 600_000
    resolver = ScriptedResolver({"big.example": [[PUBLIC_V4]]})
    backend = RecordingBackend(lambda address, port, raw: http_response(200, body, {"Content-Type": "text/html"}))
    fetcher = make_fetcher(resolver, backend)
    result = run(fetcher.get("http://big.example/", max_bytes=500_000, timeout_s=5, truncate=True))
    assert len(result.content) == 500_000 and result.truncated
    with pytest.raises(FetchError) as caught:
        run(fetcher.get("http://big.example/", max_bytes=500_000, timeout_s=5, truncate=False))
    assert (caught.value.http_status, caught.value.code) == (413, "payload_too_large")
    exact = run(fetcher.get("http://big.example/", max_bytes=600_000, timeout_s=5))
    assert len(exact.content) == 600_000 and not exact.truncated  # negative control


def test_compressed_bodies_are_capped_after_decompression():
    resolver = ScriptedResolver({"zip.example": [[PUBLIC_V4]]})
    bomb = gzip.compress(b"\x00" * 5_000_000)
    backend = RecordingBackend(lambda a, p, r: http_response(200, bomb, {"Content-Type": "text/html", "Content-Encoding": "gzip"}))
    result = run(make_fetcher(resolver, backend).get("http://zip.example/", max_bytes=100_000, timeout_s=5, truncate=True))
    assert len(result.content) == 100_000 and result.truncated
    small = gzip.compress(PAGE)
    backend = RecordingBackend(lambda a, p, r: http_response(200, small, {"Content-Type": "text/html", "Content-Encoding": "gzip"}))
    assert run(make_fetcher(resolver, backend).get("http://zip.example/", max_bytes=100_000, timeout_s=5)).content == PAGE


def test_slow_destinations_time_out():
    async def slow(address, port, raw):
        await asyncio.sleep(5)
        return page(address, port, raw)

    resolver, backend = ScriptedResolver({"slow.example": [[PUBLIC_V4]]}), RecordingBackend(slow)
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("http://slow.example/", max_bytes=1000, timeout_s=0.2))
    assert (caught.value.http_status, caught.value.code) == (504, "fetch_timeout")


def test_unresolvable_hosts_fail_cleanly():
    resolver, backend = ScriptedResolver({}), RecordingBackend(page)
    with pytest.raises(FetchError) as caught:
        run(make_fetcher(resolver, backend).get("http://nowhere.example/", max_bytes=1000, timeout_s=5))
    assert (caught.value.http_status, caught.value.code) == (502, "fetch_failed")


def test_pinned_backend_refuses_any_other_host_or_port():
    backend = RecordingBackend(page)
    pinned = PinnedNetworkBackend(Target("http://a.example/", "a.example", 80, PUBLIC_V4), backend)
    with pytest.raises(httpcore.ConnectError):
        run(pinned.connect_tcp("b.example", 80))
    with pytest.raises(httpcore.ConnectError):
        run(pinned.connect_tcp("a.example", 443))
    with pytest.raises(httpcore.ConnectError):
        run(pinned.connect_unix_socket("/run/other.sock"))
    run(pinned.connect_tcp("a.example", 80))
    assert backend.connections == [(PUBLIC_V4, 80)]
