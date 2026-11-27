"""Reading a post on X through X's oEmbed endpoint (world/x_post.py, `POST /v1/x/post`).

The oEmbed server here is a fake at the network layer: a scripted resolver for
publish.twitter.com and a backend that answers the raw HTTP request, so the real safe fetcher,
TLS name, redirect rules and size caps are all exercised. Nothing reaches the internet.
"""
from __future__ import annotations

import json
import logging
import ssl
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from starling_server.services.world.x_post import (
    StatusLink,
    parse_oembed,
    parse_status_url,
    snowflake_time,
)

from .fakes_net import RecordingBackend, ScriptedResolver, http_response, request_line_and_host

PUBLIC_V4 = "93.184.216.34"
STATUS_ID = "1851234567890123456"
OEMBED_HTML = (
    '<blockquote class="twitter-tweet" data-dnt="true"><p lang="en" dir="ltr">Shipping beats polishing. '
    'Ten small releases taught us more than one big one.<br><br>Notes: <a href="https://t.co/AbCdEf123">'
    'https://t.co/AbCdEf123</a> <a href="https://twitter.com/hashtag/buildinpublic?src=hash">#buildinpublic</a> '
    '<a href="https://t.co/PiC0000001">pic.twitter.com/PiC0000001</a></p>&mdash; Dana Example (@dana_example) '
    f'<a href="https://twitter.com/dana_example/status/{STATUS_ID}?ref_src=twsrc%5Etfw">October 29, 2026</a>'
    '</blockquote>'
)
OEMBED_JSON = {
    "url": f"https://twitter.com/dana_example/status/{STATUS_ID}",
    "author_name": "Dana Example",
    "author_url": "https://twitter.com/dana_example",
    "html": OEMBED_HTML,
    "width": 550, "height": None, "type": "rich", "cache_age": "3153600000",
    "provider_name": "Twitter", "provider_url": "https://twitter.com", "version": "1.0",
}


class FakeOembed:
    """Answers GET /oembed like publish.twitter.com and records each request it was sent."""

    def __init__(self, status: int = 200, body: object = None, *, raw: bytes | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = OEMBED_JSON if body is None else body
        self.raw = raw
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, address: str, port: int, raw: bytes) -> bytes:
        line, host = request_line_and_host(raw)
        self.requests.append((line, host))
        content = self.raw if self.raw is not None else json.dumps(self.body).encode("utf-8")
        return http_response(self.status, content if self.status == 200 else b"", self.headers)


def oembed_services(make_services, fake: FakeOembed):
    resolver = ScriptedResolver({"publish.twitter.com": [[PUBLIC_V4]], "internal.example": [["10.0.0.9"]]})
    backend = RecordingBackend(fake)
    services = make_services(resolver=resolver, network_backend=backend,
                             ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    return services, backend


def post(services, url, **headers):
    return services.client.post("/v1/x/post", json={"url": url}, headers={**services.auth(), **headers})


# --------------------------------------------------------------------------- links


@pytest.mark.parametrize("text, expected", [
    (f"https://x.com/dana_example/status/{STATUS_ID}", ("dana_example", STATUS_ID)),
    (f"look at this https://twitter.com/Dana_Example/status/{STATUS_ID}?s=20&t=abc", ("Dana_Example", STATUS_ID)),
    (f"https://mobile.twitter.com/dana_example/statuses/{STATUS_ID}", ("dana_example", STATUS_ID)),
    (f"x.com/i/web/status/{STATUS_ID}", (None, STATUS_ID)),
    (f"https://x.com/i/status/{STATUS_ID}", (None, STATUS_ID)),
    (f"https://www.x.com/dana_example/status/{STATUS_ID}/photo/1", ("dana_example", STATUS_ID)),
])
def test_links_to_one_post_are_recognised(text, expected):
    link = parse_status_url(text)
    assert link is not None and (link.handle, link.status_id) == expected
    assert link.url == f"https://x.com/{expected[0] or 'i'}/status/{STATUS_ID}"


@pytest.mark.parametrize("text", [
    "", "no link here", "https://x.com/dana_example", "https://example.com/dana/status/123",
    "https://x.com.evil.example/dana/status/123", "https://x.com/dana_example/status/", None, 5,
    "https://evilx.com/dana/status/123", "https://notx.com/dana/status/123",
])
def test_anything_else_is_not_a_post_link(text):
    assert parse_status_url(text) is None


def test_a_post_id_carries_its_creation_time():
    created = snowflake_time("1851234567890123456", now=2_000_000_000)
    assert created is not None and 1_729_000_000 < created < 1_731_000_000
    assert snowflake_time("12345") is None and snowflake_time("not a number") is None
    assert snowflake_time(str(1 << 62), now=1_800_000_000) is None, "a time in the future is not believed"


def test_the_blockquote_becomes_plain_text_author_and_links():
    post = parse_oembed(OEMBED_JSON, StatusLink(STATUS_ID, "someone_else"))
    assert post["text"] == ("Shipping beats polishing. Ten small releases taught us more than one big one.\n\n"
                            "Notes: https://t.co/AbCdEf123 #buildinpublic")
    assert post["author_name"] == "Dana Example" and post["author_handle"] == "dana_example"
    assert post["url"] == f"https://x.com/dana_example/status/{STATUS_ID}" and post["lang"] == "en"
    assert post["links"] == ["https://t.co/AbCdEf123"] and post["has_media"] is True


def test_a_photo_only_post_has_no_text_and_hostile_markup_stays_text():
    photo = {**OEMBED_JSON, "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">'
             '<a href="https://t.co/x">pic.twitter.com/x</a></p>&mdash; Dana</blockquote>'}
    assert parse_oembed(photo, StatusLink(STATUS_ID, None))["text"] == ""
    hostile = {**OEMBED_JSON, "author_url": "https://evil.example/admin", "html":
               '<blockquote><p lang="en"><script>alert(1)</script>&lt;b&gt;bold&lt;/b&gt;</p></blockquote>'}
    post = parse_oembed(hostile, StatusLink(STATUS_ID, None))
    assert post["text"] == "alert(1)<b>bold</b>" and post["author_handle"] is None


# --------------------------------------------------------------------------- the route


def test_the_route_asks_x_oembed_for_the_post_and_nothing_else(make_services, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeOembed()
    services, backend = oembed_services(make_services, fake)
    response = post(services, f"Read this https://x.com/dana_example/status/{STATUS_ID}?s=20&t=TRACKER-CANARY")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"].startswith("Shipping beats polishing.") and body["author_handle"] == "dana_example"
    assert body["status_id"] == STATUS_ID and body["cached"] is False
    assert backend.connections == [(PUBLIC_V4, 443)] and backend.tls_server_names == ["publish.twitter.com"]
    line, host = fake.requests[0]
    assert host == "publish.twitter.com"
    target = urlsplit(line.split(" ")[1])
    query = parse_qs(target.query)
    assert target.path == "/oembed"
    assert query == {"url": [f"https://twitter.com/dana_example/status/{STATUS_ID}"], "omit_script": ["1"], "dnt": ["true"]}
    assert "TRACKER-CANARY" not in unquote(line), "only the post's id and handle leave the service"
    assert STATUS_ID not in caplog.text and "dana_example" not in caplog.text


def test_answers_are_cached_per_post(make_services):
    fake = FakeOembed()
    services, _ = oembed_services(make_services, fake)
    assert post(services, f"https://x.com/dana_example/status/{STATUS_ID}").json()["cached"] is False
    again = post(services, f"https://twitter.com/i/web/status/{STATUS_ID}")
    assert again.status_code == 200 and again.json()["cached"] is True and len(fake.requests) == 1
    services.clock.advance(601)
    assert post(services, f"https://x.com/dana_example/status/{STATUS_ID}").json()["cached"] is False
    assert len(fake.requests) == 2


def test_a_post_x_will_not_show_is_post_unavailable_and_briefly_remembered(make_services):
    fake = FakeOembed(status=404)
    services, _ = oembed_services(make_services, fake)
    for _ in range(2):
        missing = post(services, f"https://x.com/gone/status/{STATUS_ID}")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "post_unavailable"
    assert len(fake.requests) == 1
    services.clock.advance(121)
    post(services, f"https://x.com/gone/status/{STATUS_ID}")
    assert len(fake.requests) == 2


@pytest.mark.parametrize("fake, status, code", [
    (FakeOembed(status=500), 502, "fetch_failed"),
    (FakeOembed(raw=b"<html>not json</html>"), 502, "fetch_failed"),
    (FakeOembed(body={"type": "rich"}), 502, "fetch_failed"),
    (FakeOembed(raw=b"{" + b" " * (70 * 1024) + b"}"), 502, "fetch_failed"),
])
def test_broken_answers_are_failures_not_posts(make_services, fake, status, code):
    services, _ = oembed_services(make_services, fake)
    response = post(services, f"https://x.com/dana_example/status/{STATUS_ID}")
    assert response.status_code == status and response.json()["error"]["code"] == code


def test_a_redirect_to_a_private_address_is_refused(make_services):
    fake = FakeOembed(status=302, headers={"Location": "http://internal.example/oembed"})
    services, backend = oembed_services(make_services, fake)
    response = post(services, f"https://x.com/dana_example/status/{STATUS_ID}")
    assert response.status_code == 403 and response.json()["error"]["code"] == "destination_blocked"
    assert all(address == PUBLIC_V4 for address, _ in backend.connections)


def test_only_the_gateway_may_ask_and_only_for_post_links(make_services):
    fake = FakeOembed()
    services, _ = oembed_services(make_services, fake)
    account = services.add_account()
    engine = services.client.post("/v1/x/post", json={"url": f"https://x.com/a/status/{STATUS_ID}"},
                                  headers=services.auth(account))
    assert engine.status_code == 401, "an engine token cannot use the gateway's lookup"
    assert services.client.post("/v1/x/post", json={"url": f"https://x.com/a/status/{STATUS_ID}"}).status_code == 401
    assert post(services, "https://example.com/article").status_code == 400
    assert services.client.post("/v1/x/post", json={"url": "x", "extra": 1}, headers=services.auth()).status_code == 400
    assert fake.requests == []


def test_lookups_have_a_service_wide_ceiling(make_services):
    from starling_server.services.world.routes import FetchRateLimiter

    fake = FakeOembed()
    services, _ = oembed_services(make_services, fake)
    services.app.state.world.x_post_limiter = FetchRateLimiter(per_minute=2, monotonic=services.clock)
    for number in range(2):
        assert post(services, f"https://x.com/a/status/{STATUS_ID}{number}").status_code == 200
    limited = post(services, f"https://x.com/a/status/{STATUS_ID}9")
    assert limited.status_code == 429 and limited.headers["retry-after"]


def test_offline_world_reads_nothing(make_services):
    services = make_services(overrides={"STARLING_WORLD_OFFLINE": "1"})
    response = post(services, f"https://x.com/dana_example/status/{STATUS_ID}")
    assert response.status_code == 502 and response.json()["error"]["code"] == "fetch_failed"
