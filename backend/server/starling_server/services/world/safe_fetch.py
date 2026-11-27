"""Outbound HTTP for the world service that cannot be aimed at private networks.

For every hop (the first request and each redirect, at most `max_redirects`):

1. the URL must be absolute `http` or `https` on port 80 or 443, without credentials;
2. the host is resolved once and every answer must be a public unicast address. Private, loopback,
   link-local, multicast, reserved, unspecified, shared (100.64/10) and documentation ranges are
   refused, including their IPv4-mapped, 6to4 and Teredo forms;
3. the connection goes to the first validated address. A pinned network backend substitutes that
   address when the socket is opened, so no second DNS lookup happens. The URL keeps the host name,
   so the Host header, TLS SNI and certificate verification use the real name.

Bodies are read with a byte cap measured after decompression (gzip and deflate are inflated
incrementally with a bound). Proxy settings from the environment are ignored. Nothing here logs URLs.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import zlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import httpcore
import httpx

from ..errors import ServiceError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_PORTS = {"http": 80, "https": 443}
ALLOWED_PORTS = frozenset({80, 443})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_URL_LENGTH = 2048
BLOCKED_HOSTNAMES = frozenset({"localhost"})
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_GLOBAL_UNICAST_V6 = ipaddress.IPv6Network("2000::/3")
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")

Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


class FetchError(ServiceError):
    """A world-service fetch that was refused or failed."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes
    truncated: bool = False


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    port: int
    address: str


def public_address(value: str) -> str | None:
    """The normalized address when `value` is a public unicast IP address, else None."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return None
        if ip.ipv4_mapped is not None:
            return public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            # 6to4 (2002::/16) is refused outright, whatever IPv4 address it wraps. It is deprecated
            # (RFC 7526), nothing a fetch needs is reachable only that way, and it is a tidy wrapper
            # for an address the checks below would otherwise judge on their own. It also has to be
            # decided HERE rather than left to `is_global`: CPython changed that answer in a patch
            # release (3.11.9 says globally reachable, 3.11.16 says not), so leaving it to the
            # standard library made a security control depend on which Python the image happened to
            # ship. Found by the first run of this suite inside the Linux image.
            return None
        if ip.teredo is not None:
            return None
        if ip not in _GLOBAL_UNICAST_V6:
            return None
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return None
    return str(ip)


def _ip_literal(host: str) -> str | None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    answers: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in answers:
            answers.append(address)
    return answers


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Opens sockets only to the validated address of one validated host and port."""

    def __init__(self, target: Target, inner: httpcore.AsyncNetworkBackend) -> None:
        self._target = target
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower().rstrip(".") != self._target.host or port != self._target.port:
            raise httpcore.ConnectError("the connection target does not match the validated destination")
        return await self._inner.connect_tcp(
            self._target.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool uses the pinned network backend (HTTP/1.1 only)."""

    def __init__(self, ssl_context: ssl.SSLContext, backend: httpcore.AsyncNetworkBackend) -> None:
        super().__init__(verify=ssl_context, trust_env=False, retries=0)
        if not hasattr(self, "_pool"):
            raise RuntimeError("this httpx version does not expose its connection pool; refusing to fetch")
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


class _Inflater:
    def __init__(self, encoding: str) -> None:
        if encoding not in ("gzip", "x-gzip", "deflate"):
            raise FetchError(502, "fetch_failed", "the destination used an unsupported content encoding")
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib wrapper

    def feed(self, data: bytes, budget: int) -> bytes:
        """Inflate at most `budget` bytes; input beyond the budget is dropped."""
        try:
            produced = [self._decompressor.decompress(data, max(1, budget))]
            size = len(produced[0])
            while self._decompressor.unconsumed_tail and size < budget:
                more = self._decompressor.decompress(self._decompressor.unconsumed_tail, max(1, budget - size))
                produced.append(more)
                size += len(more)
            return b"".join(produced)
        except zlib.error:
            raise FetchError(502, "fetch_failed", "the response could not be decoded") from None

    def flush(self) -> bytes:
        try:
            return self._decompressor.flush()
        except zlib.error:
            raise FetchError(502, "fetch_failed", "the response could not be decoded") from None


def _too_large() -> FetchError:
    return FetchError(413, "payload_too_large", "the response is larger than allowed")


async def _read_body(response: httpx.Response, max_bytes: int, truncate: bool) -> tuple[bytes, bool]:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    inflater: _Inflater | None = None
    if encoding in ("", "identity"):
        declared = response.headers.get("content-length", "").strip()
        if not truncate and declared.isdigit() and int(declared) > max_bytes:
            raise _too_large()
    else:
        inflater = _Inflater(encoding)
    chunks: list[bytes] = []
    total = 0
    async for raw in response.aiter_raw():
        piece = inflater.feed(raw, max_bytes + 1 - total) if inflater is not None else raw
        if total + len(piece) > max_bytes:
            if truncate:
                chunks.append(piece[: max_bytes - total])
                return b"".join(chunks), True
            raise _too_large()
        chunks.append(piece)
        total += len(piece)
    if inflater is not None:
        tail = inflater.flush()
        if total + len(tail) > max_bytes:
            if truncate:
                chunks.append(tail[: max_bytes - total])
                return b"".join(chunks), True
            raise _too_large()
        chunks.append(tail)
    return b"".join(chunks), False


class SafeFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
        user_agent: str = USER_AGENT,
        offline: bool = False,
        fixtures: Any = None,
    ) -> None:
        self._resolver = resolver or system_resolver
        self._backend = network_backend or httpcore.AnyIOBackend()
        self._ssl_context = ssl_context
        self._user_agent = user_agent
        # STARLING_WORLD_OFFLINE (development and e2e only, CONTRACTS 4.2 and 12): every fetch
        # fails at once with the existing `fetch_failed` code, before any name resolution or
        # socket, so a test run makes no outbound connection at all.
        self.offline = bool(offline)
        # STARLING_WORLD_FIXTURES (e2e only, `world/fixtures.py`): an offline fetcher answers the
        # pages listed in the fixture file, still without any connection.
        self._fixtures = fixtures if self.offline else None

    def _ssl(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = httpx.create_ssl_context(trust_env=False)
        return self._ssl_context

    async def get(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_s: float,
        max_redirects: int = 3,
        accept: str = "*/*",
        truncate: bool = False,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """GET `url` following at most `max_redirects` validated redirects within `timeout_s` in total."""
        if self.offline:
            fixture = self._fixtures.page(url) if self._fixtures is not None else None
            if fixture is not None:
                return fixture
            raise FetchError(502, "fetch_failed", "the world service is offline; nothing is fetched")
        try:
            async with asyncio.timeout(timeout_s):
                return await self._follow(
                    url,
                    max_bytes=max_bytes,
                    max_redirects=max_redirects,
                    accept=accept,
                    truncate=truncate,
                    headers=headers,
                    timeout_s=timeout_s,
                )
        except TimeoutError:
            raise FetchError(504, "fetch_timeout", "the destination did not answer in time") from None

    async def _follow(
        self,
        url: str,
        *,
        max_bytes: int,
        max_redirects: int,
        accept: str,
        truncate: bool,
        headers: dict[str, str] | None,
        timeout_s: float,
    ) -> FetchResult:
        current = url
        redirects = 0
        while True:
            target = await self.validate(current)
            result = await self._request(
                target, max_bytes=max_bytes, accept=accept, truncate=truncate, headers=headers, timeout_s=timeout_s
            )
            if result.status not in REDIRECT_STATUSES:
                return result
            location = (result.headers.get("location") or "").strip()
            if not location:
                raise FetchError(502, "fetch_failed", "the destination redirected without a location")
            if redirects >= max_redirects:
                raise FetchError(502, "fetch_failed", "the destination redirected too many times")
            try:
                current = str(httpx.URL(target.url).join(location))
            except (httpx.InvalidURL, ValueError, TypeError):
                raise FetchError(502, "fetch_failed", "the destination sent an invalid redirect") from None
            redirects += 1

    async def validate(self, raw_url: object) -> Target:
        """Check the URL and resolve its host once; return where the connection must go."""
        if not isinstance(raw_url, str) or not raw_url or len(raw_url) > MAX_URL_LENGTH:
            raise FetchError(400, "bad_request", "url must be an absolute http or https URL")
        try:
            url = httpx.URL(raw_url)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise FetchError(400, "bad_request", "url is not a valid URL") from None
        if url.scheme not in DEFAULT_PORTS:
            raise FetchError(400, "bad_request", "only http and https URLs are allowed")
        if url.userinfo:
            raise FetchError(400, "bad_request", "URLs with credentials are not allowed")
        try:
            host = url.raw_host.decode("ascii").lower().rstrip(".")
        except UnicodeDecodeError:
            raise FetchError(400, "bad_request", "the URL host is not valid") from None
        if not host:
            raise FetchError(400, "bad_request", "url must include a host")
        if "%" in host:
            # A percent-encoded host (`%31%32%37%2e%30%2e%30%2e%31`) is neither an IP literal nor a
            # numeric label to the checks below, so whether it reaches 127.0.0.1 would depend on the
            # resolver. No real host name contains a percent sign.
            raise FetchError(400, "bad_request", "the URL host is not valid")
        port = url.port or DEFAULT_PORTS[url.scheme]
        if port not in ALLOWED_PORTS:
            raise FetchError(400, "bad_request", "only the standard ports 80 and 443 are allowed")

        literal = _ip_literal(host)
        if literal is not None:
            address = public_address(literal)
            if address is None:
                raise FetchError(403, "destination_blocked", "the destination address is not public")
            return Target(str(url), host, port, address)
        if ":" in host:
            raise FetchError(400, "bad_request", "the URL host is not valid")
        if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
            raise FetchError(403, "destination_blocked", "numeric host names are not allowed")
        if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
            raise FetchError(403, "destination_blocked", "the destination host is not public")
        try:
            answers = [str(answer) for answer in await self._resolver(host, port)]
        except (OSError, UnicodeError):
            raise FetchError(502, "fetch_failed", "the destination host could not be resolved") from None
        if not answers:
            raise FetchError(502, "fetch_failed", "the destination host could not be resolved")
        validated = [public_address(answer) for answer in answers]
        if any(address is None for address in validated):
            raise FetchError(403, "destination_blocked", "the destination host resolves to a non-public address")
        return Target(str(url), host, port, str(validated[0]))

    async def _request(
        self,
        target: Target,
        *,
        max_bytes: int,
        accept: str,
        truncate: bool,
        headers: dict[str, str] | None,
        timeout_s: float,
    ) -> FetchResult:
        transport = _PinnedTransport(self._ssl(), PinnedNetworkBackend(target, self._backend))
        request_headers = {
            "User-Agent": self._user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        }
        request_headers.update(headers or {})
        try:
            async with httpx.AsyncClient(
                transport=transport, follow_redirects=False, trust_env=False, timeout=httpx.Timeout(timeout_s)
            ) as client:
                async with client.stream("GET", target.url, headers=request_headers) as response:
                    if 200 <= response.status_code < 300:
                        content, truncated = await _read_body(response, max_bytes, truncate)
                    else:
                        content, truncated = b"", False
                    return FetchResult(
                        url=target.url,
                        status=response.status_code,
                        headers=response.headers,
                        content=content,
                        truncated=truncated,
                    )
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError(504, "fetch_timeout", "the destination did not answer in time") from None
        except httpx.HTTPError:
            raise FetchError(502, "fetch_failed", "the destination could not be reached") from None
