"""Two-customer canary (security invariant 7): customer A's requests, captures and files only
ever reach A's engine and A's data folder, never B's, including after eviction and respawn."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

from .support import insert_account, make_pool, run, served
from .test_streams import Stream, chat


def call(h, method: str, path: str, name: str, key: str | None = None, body=None) -> httpx.Response:
    with httpx.Client(timeout=60, trust_env=False) as client:
        return client.request(method, h.url(path), json=body, headers=h.headers(name, key))


def files_containing(root: Path, needle: str) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and needle.encode("utf-8") in path.read_bytes()]


def test_two_customers_never_share_an_engine_or_a_data_folder(harness_factory, fake_usage, slots, settings):
    h = harness_factory(("a", "b"), max_live=1, passive=True)
    a_id, b_id = h.account_id("a"), h.account_id("b")
    a_dir, b_dir = settings.tenants_dir / "1", settings.tenants_dir / "2"
    canary_a = [f"canary-a-{n}-{uuid.uuid4().hex}" for n in range(4)]
    canary_b = [f"canary-b-{n}-{uuid.uuid4().hex}" for n in range(2)]

    first = call(h, "POST", "/api/v1/assist", "a", str(uuid.uuid4()),
                 {"mode": "context", "context": {"text": canary_a[0]}, "nonce": canary_a[0]})
    assert first.status_code == 200
    assert first.json()["served_by"] == {"tenant_id": a_id, "app_dir": str(a_dir), "pid": first.json()["served_by"]["pid"]}
    first_pid = first.json()["served_by"]["pid"]

    stream = Stream(h, name="b", body={**chat(tokens=[canary_b[0]]), "nonce": canary_b[0]})
    stream.rest()
    final_b = stream.first("final")[1]["response"]
    assert final_b["served_by"]["tenant_id"] == b_id and final_b["served_by"]["app_dir"] == str(b_dir)
    assert not h.pool.is_live(a_id)  # A was evicted to make room for B

    queued = call(h, "POST", "/api/v1/snapshots/metrics", "a", body={"post_id": "1", "nonce": canary_a[1]})
    assert queued.status_code == 202  # A's engine is down: spooled, not handed to B's engine

    again = call(h, "GET", "/api/v1/own-material", "a")
    assert again.status_code == 200
    respawned = again.json()["served_by"]
    assert respawned["tenant_id"] == a_id and respawned["app_dir"] == str(a_dir) and respawned["pid"] != first_pid
    replayed = call(h, "POST", "/api/v1/outcomes", "a", body={"nonce": canary_a[2]})
    assert replayed.status_code == 200 and replayed.json()["served_by"]["tenant_id"] == a_id
    b_again = call(h, "POST", "/api/v1/own-material", "b", str(uuid.uuid4()), {"kind": "voice", "text": "b", "nonce": canary_b[1]})
    assert b_again.json()["served_by"]["tenant_id"] == b_id
    a_stream = Stream(h, name="a", body={**chat(tokens=[canary_a[3]]), "nonce": canary_a[3]})
    a_stream.rest()
    assert a_stream.first("final")[1]["response"]["served_by"]["tenant_id"] == a_id

    a_lines, b_lines = served(settings, 1), served(settings, 2)
    assert {line["tenant_id"] for line in a_lines} == {a_id}
    assert {line["tenant_id"] for line in b_lines} == {b_id}
    assert set(canary_a) <= {line["nonce"] for line in a_lines}
    assert set(canary_b) <= {line["nonce"] for line in b_lines}
    for secret in canary_a + [a_id]:
        assert files_containing(b_dir, secret) == []
        assert files_containing(settings.data_dir / "spool", secret) == []  # A's spool was drained
    for secret in canary_b + [b_id]:
        assert files_containing(a_dir, secret) == []
    # Negative control: the scan does find A's canary in A's own folder.
    assert files_containing(a_dir, canary_a[0])
    # Each engine was given a services token for its own customer only.
    assert json.loads((a_dir / "stdin.json").read_text())["services_token_subject"] == f"t-{a_id}"
    assert json.loads((b_dir / "stdin.json").read_text())["services_token_subject"] == f"t-{b_id}"
    # Responses never carried the other customer's text.
    assert canary_b[0] not in a_stream.first("final")[1]["response"].get("answer", "")
    assert all(canary_a[0] not in json.dumps(event) for _, event in stream.events)


def test_one_customers_worker_token_does_not_open_another_customers_engine(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database, max_live=2)
        await pool.start(maintenance=False)
        try:
            handle_a = await pool.ensure(a, 1)
            await pool.ensure(b, 2)
            a_url = "http://" + handle_a.listen[len("tcp:"):] + "/livez"
            with_b_token = await pool._engines[b].client.get(a_url)
            with_a_token = await pool._engines[a].client.get(a_url)
            return with_b_token.status_code, with_a_token.status_code
        finally:
            await pool.close()

    with_b_token, with_a_token = run(scenario())
    assert with_b_token == 404
    assert with_a_token == 200  # control
