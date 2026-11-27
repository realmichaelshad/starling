"""POST /api/v1/assist/stream: accepted line, heartbeats, tokens and exposure, settlement,
cancel, client disconnect (slot held until the engine finishes), time limit, broken engine."""
from __future__ import annotations

import json
import time
import uuid

import httpx

from .support import read_jsonl, served, wait_until


class Stream:
    """One streamed request read line by line, with the arrival time of every event."""

    def __init__(self, h, *, name: str = "a", key: str | None = None, body: dict | None = None,
                 time_zone: str | None = "Europe/London") -> None:
        self.key = key or str(uuid.uuid4())
        self.client = httpx.Client(timeout=httpx.Timeout(60.0), trust_env=False)
        self.started = time.monotonic()
        self._context = self.client.stream("POST", h.url("/api/v1/assist/stream"), json=body,
                                           headers=h.headers(name, self.key, time_zone))
        self.response = self._context.__enter__()
        self._lines = self.response.iter_lines()
        self.events: list[tuple[float, dict]] = []

    def next(self) -> dict:
        for line in self._lines:
            if line.strip():
                event = json.loads(line)
                self.events.append((time.monotonic(), event))
                return event
        raise EOFError("stream ended")

    def until(self, kind: str) -> dict:
        while True:
            event = self.next()
            if event["event"] == kind:
                return event

    def rest(self) -> list[dict]:
        try:
            while True:
                self.next()
        except EOFError:
            pass
        self.close()
        return self.kinds()

    def kinds(self) -> list[str]:
        return [event["event"] for _, event in self.events]

    def first(self, kind: str) -> tuple[float, dict]:
        return next((at, event) for at, event in self.events if event["event"] == kind)

    def close(self) -> None:
        self._context.__exit__(None, None, None)
        self.client.close()


def chat(**fake) -> dict:
    return {"mode": "chat", "message": "Draft a reply", "fake": fake}


def cancels(h) -> list[dict]:
    return read_jsonl(h.settings.tenants_dir / "1" / "cancels.jsonl")


def test_accepted_line_arrives_before_the_engine_has_started(harness_factory, fake_usage, slots):
    h = harness_factory(("a",), fake_args=("--ready-delay", "1.5"))
    stream = Stream(h, body=chat(tokens=["hi"]))
    first = stream.next()
    accepted_after = time.monotonic() - stream.started
    engine_had_request = bool(served(h.settings, 1))
    assert first == {"event": "status", "stage": "accepted", "request_id": stream.key}
    assert accepted_after < 1.0 and not engine_had_request
    assert stream.response.headers["content-type"].startswith("application/x-ndjson")
    assert stream.response.headers["cache-control"] == "no-store, no-transform"
    assert stream.response.headers["x-accel-buffering"] == "no"
    kinds = stream.rest()
    assert kinds[-2:] == ["usage", "final"]
    assert served(h.settings, 1)  # control: the engine did serve the request, later


def test_heartbeat_after_silence_but_not_while_output_flows(harness_factory, fake_usage, slots):
    h = harness_factory(("a",), heartbeat_s=0.3)
    quiet = Stream(h, body=chat(tokens=["a", "b"], silent_s=1.5))
    kinds = quiet.rest()
    assert "heartbeat" in kinds and kinds.index("heartbeat") < kinds.index("token")
    busy = Stream(h, body=chat(tokens=["x"] * 5, token_delay_s=0.02))  # engine already warm
    kinds = busy.rest()
    assert "heartbeat" not in kinds and kinds.count("token") == 5  # control


def test_tokens_are_forwarded_exposure_is_recorded_first_and_a_usable_final_commits(harness, fake_usage, slots):
    stream = Stream(harness, body=chat(tokens=["Hel", "lo"], answer="Hello"), time_zone="Asia/Tokyo")
    kinds = stream.rest()
    assert kinds == ["status", "status", "token", "token", "usage", "final"]
    token_at, token = stream.first("token")
    assert token == {"event": "token", "text": "Hel"}
    assert fake_usage.names() == ["reserve", "activate", "mark_exposed", "finish"]
    assert fake_usage.call_time("mark_exposed") <= token_at
    final = stream.first("final")[1]["response"]
    assert final["answer"] == "Hello" and final["served_by"]["tenant_id"] == harness.account_id("a")
    row = fake_usage.only_row()
    assert row["state"] == "committed" and json.loads(row["result_json"]) == final
    line = served(harness.settings, 1)[-1]
    assert line["request_id"] == row["id"] and line["time_zone"] == "Asia/Tokyo"

    replay = Stream(harness, key=stream.key, body=chat(tokens=["Hel", "lo"], answer="Hello"))
    assert replay.rest() == ["usage", "final"]
    assert replay.first("final")[1]["response"] == final
    assert len(served(harness.settings, 1)) == 1  # the engine was not called again
    with httpx.Client(trust_env=False) as client:
        stored = client.get(harness.url(f"/api/v1/requests/{stream.key}"), headers=harness.headers("a"))
        other = client.get(harness.url(f"/api/v1/requests/{stream.key}"), headers=harness.headers("b"))
    assert stored.status_code == 200 and stored.json()["state"] == "done" and stored.json()["response"] == final
    assert other.status_code == 404  # results are scoped to the account


def test_engine_errors_release_and_are_mapped(harness, fake_usage, slots):
    limited = Stream(harness, body=chat(script="error", code="ai_rate_limited", retry_after_s=7))
    assert limited.rest() == ["status", "status", "usage", "error"]
    error = limited.first("error")[1]["error"]
    assert error["code"] == "ai_unavailable" and error["retry_after_s"] == 7
    paused = Stream(harness, body=chat(script="error", code="budget_exhausted"))
    paused.rest()
    assert paused.first("error")[1]["error"]["code"] == "service_paused"
    empty = Stream(harness, body=chat(script="empty"))
    assert empty.rest()[-2:] == ["usage", "final"]
    states = sorted((row["state"], row["reason"]) for row in fake_usage.by_id.values())
    assert states == [("released", "engine_error"), ("released", "engine_error"), ("released", "unusable")]
    assert "mark_exposed" not in fake_usage.names()


def test_cancel_before_any_token_releases(harness, fake_usage, slots):
    stream = Stream(harness, body=chat(script="hang", hang_s=30))
    stream.until("status")
    stream.until("status")  # the engine's own stage: it has the request
    with httpx.Client(trust_env=False) as client:
        refused = client.post(harness.url(f"/api/v1/requests/{stream.key}/cancel"), headers=harness.headers("b", str(uuid.uuid4())))
        cancelled = client.post(harness.url(f"/api/v1/requests/{stream.key}/cancel"), headers=harness.headers("a", str(uuid.uuid4())))
    assert refused.status_code == 404  # another account cannot cancel it
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
    assert stream.rest()[-2:] == ["usage", "final"]
    assert stream.first("final")[1]["response"]["cancelled"] is True
    assert "used_after_stop" not in stream.first("final")[1]["response"]
    row = fake_usage.only_row()
    assert row["state"] == "released" and "mark_exposed" not in fake_usage.names()
    assert [item["request_id"] for item in cancels(harness)] == [row["id"]]


def test_cancel_after_a_token_commits(harness, fake_usage, slots):
    stream = Stream(harness, body=chat(script="tokens_then_hang", tokens=["one"], hang_s=30))
    stream.until("token")
    with httpx.Client(trust_env=False) as client:
        client.post(harness.url(f"/api/v1/requests/{stream.key}/cancel"), headers=harness.headers("a", str(uuid.uuid4())))
    assert stream.rest()[-2:] == ["usage", "final"]
    assert fake_usage.only_row()["state"] == "committed"
    # The customer is told why a stopped draft still used one.
    assert stream.first("final")[1]["response"]["used_after_stop"] is True


def test_a_preview_then_withheld_or_failed_uses_nothing(harness, fake_usage, slots):
    withheld = Stream(harness, body=chat(script="tokens_then_withheld", tokens=["a preview"]))
    assert withheld.rest()[-2:] == ["usage", "final"]
    assert "used_after_stop" not in withheld.first("final")[1]["response"]
    failed = Stream(harness, body=chat(script="error", tokens_first=True, tokens=["half"], code="ai_unavailable"))
    assert failed.rest()[-2:] == ["usage", "error"]
    rows = sorted((row["exposed"], row["state"], row["reason"]) for row in fake_usage.by_id.values())
    assert rows == [(True, "released", "engine_error"), (True, "released", "unusable")]


def test_client_disconnect_keeps_the_slot_until_the_engine_finishes(harness, fake_usage, slots):
    offset = time.time() - time.monotonic()
    first = Stream(harness, body=chat(script="tokens_then_hang", tokens=["partial"], hang_s=30, cancel_delay_s=1.5))
    first.until("token")
    first.close()
    closed_at = time.monotonic()
    assert wait_until(lambda: len(cancels(harness)) == 1, timeout=10)
    held_after_cancel = slots.held(harness.account_id("a"))

    second = Stream(harness, body=chat(tokens=["next"]))  # must wait for the slot
    kinds = second.rest()

    assert held_after_cancel is True
    assert wait_until(lambda: not slots.held(harness.account_id("a")), timeout=10)
    engine_end = read_jsonl(harness.settings.tenants_dir / "1" / "stream_end.jsonl")[0]
    first_exit = next(at for kind, account, at in slots.events if kind == "exit")
    assert first_exit + offset >= engine_end["at"] - 0.05  # slot freed only after the engine finished
    assert first_exit - closed_at >= 1.2
    row = next(row for row in fake_usage.by_id.values() if row["exposed"])
    assert row["state"] == "committed"  # a token was shown before the disconnect
    assert fake_usage.call_time("release") + offset >= engine_end["at"] - 0.05
    assert "queued" in kinds and kinds[-2:] == ["usage", "final"]
    assert second.first("token")[0] >= first_exit


def test_total_time_limit_cancels_the_engine_and_releases(harness_factory, fake_usage, slots):
    h = harness_factory(("a",), stream_total_s=1.0, stream_hard_wall_s=5.0)
    stream = Stream(h, body=chat(script="hang", hang_s=30, cancel_delay_s=0.5))
    kinds = stream.rest()
    elapsed = stream.first("error")[0] - stream.started
    assert kinds[-2:] == ["usage", "error"]
    assert stream.first("error")[1]["error"]["code"] == "ai_unavailable"
    # The engine would hang for 30 s. The upper bound only has to prove the limit cut it short: a cold
    # engine start on a busy machine adds seconds, and the `timeout` reason below is what tells the
    # total-time limit apart from the hard wall.
    assert 1.0 <= elapsed < 15.0
    row = fake_usage.only_row()
    assert row["state"] == "released" and row["reason"] == "timeout"
    assert [item["request_id"] for item in cancels(h)] == [row["id"]]
    assert wait_until(lambda: not slots.held(h.account_id("a")), timeout=10)


def test_a_broken_engine_connection_releases_and_counts_as_a_crash(harness, fake_usage, slots):
    stream = Stream(harness, body=chat(script="crash", tokens=[]))
    assert stream.rest()[-2:] == ["usage", "error"]
    assert stream.first("error")[1]["error"]["code"] == "ai_unavailable"
    assert fake_usage.only_row()["state"] == "released"
    assert wait_until(lambda: len(harness.pool._crashes.get(harness.account_id("a"), ())) == 1, timeout=10)


def test_a_third_request_is_refused_busy_while_one_runs_and_one_waits(harness, fake_usage, slots):
    running = Stream(harness, body=chat(script="hang", hang_s=30))
    running.until("status")
    waiting = Stream(harness, body=chat(tokens=["later"]))
    waiting.until("queued")
    third = Stream(harness, body=chat(tokens=["never"]))
    assert third.rest()[-2:] == ["usage", "error"]
    assert third.first("error")[1]["error"]["code"] == "busy"
    with httpx.Client(trust_env=False) as client:
        client.post(harness.url(f"/api/v1/requests/{running.key}/cancel"), headers=harness.headers("a", str(uuid.uuid4())))
    running.rest()
    assert waiting.rest()[-2:] == ["usage", "final"]  # control: the queued request ran


def test_a_one_shot_draft_cancelled_while_the_engine_works_is_never_charged(harness, fake_usage, slots):
    """✦ is one request, not a stream. Cancelled mid-way (the extension's Cancel, or its own 60 s
    timeout), the person is told "No draft was used", so an answer the engine sends afterwards
    must be released, never committed."""
    import threading

    key = str(uuid.uuid4())
    result: dict = {}

    def press() -> None:
        with httpx.Client(trust_env=False, timeout=30) as client:
            result["response"] = client.post(
                harness.url("/api/v1/assist"),
                json={"mode": "context", "context": {"text": "x"}, "fake": {"script": "slow", "delay_s": 2.0}},
                headers=harness.headers("a", key),
            )

    thread = threading.Thread(target=press)
    thread.start()
    assert wait_until(lambda: "activate" in fake_usage.names(), timeout=20)
    with httpx.Client(trust_env=False) as client:
        cancelled = client.post(harness.url(f"/api/v1/requests/{key}/cancel"), headers=harness.headers("a", str(uuid.uuid4())))
    assert cancelled.status_code == 200
    thread.join(timeout=30)
    body = result["response"].json()
    assert body.get("cancelled") is True
    assert fake_usage.only_row()["state"] == "released"
    assert "finish" not in fake_usage.names()
