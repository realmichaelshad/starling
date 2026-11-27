"""Journey 0: the harness is what it claims to be (local, mocked providers).

Before any journey means anything, the stack has to be the real one with only the two paid
providers replaced. These checks fail loudly if a run ever points at something else.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from e2e.tests.client import Control


def test_the_gateway_is_ready_and_has_no_configuration_problems(gateway_url: str):
    with httpx.Client(base_url=gateway_url, timeout=30.0, trust_env=False) as client:
        assert client.get("/healthz").json() == {"ok": True}
        ready = client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert ready.json()["checks"] == {"database": True, "schema": True, "templates": True, "config": True}


def test_the_public_plans_are_the_ones_in_the_plans_file(gateway_url: str, plans: dict):
    with httpx.Client(base_url=gateway_url, timeout=30.0, trust_env=False) as client:
        body = client.get("/api/v1/plans").json()
    published = {plan["code"]: plan for plan in body["plans"]}
    assert set(published) == set(plans)
    for code, plan in plans.items():
        assert published[code]["monthly_credits"] == plan["monthly_credits"]
        assert published[code]["rate_per_minute"] == plan["rate_per_minute"]
    assert not any("stripe" in key for plan in body["plans"] for key in plan), "no Stripe ids are published"


def test_both_providers_are_the_local_fakes(harness: Control, stack: dict):
    assert harness.openai_get("/health")["ok"] is True
    stripe = harness.stripe_get("/health")
    assert stripe["ok"] is True
    for url in (stack["fake_openai_url"], stack["fake_stripe_url"], stack["gateway_url"], stack["services_url"]):
        assert "127.0.0.1" in url, url
    assert stack["stripe"]["pages_url"].startswith("https://127.0.0.1:"), stack["stripe"]


def test_the_world_service_is_offline(stack: dict):
    """No news refresh and no page fetch: a run reaches nothing outside this machine."""
    log = Path(stack["log_dir"]) / "services.err.txt"
    assert log.is_file(), log
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "STARLING_WORLD_OFFLINE is set" in text, text[-2000:]


def test_every_process_of_the_run_logs_into_the_run_folder(stack: dict):
    log_dir = Path(stack["log_dir"])
    for name in ("gateway", "services", "fake-openai", "fake-stripe"):
        assert (log_dir / f"{name}.out.txt").is_file() or (log_dir / f"{name}.err.txt").is_file(), name
    assert (log_dir / "stripe-setup.txt").is_file()
    assert Path(stack["control_db"]).is_file()
    assert Path(stack["run_dir"]).name in str(log_dir)
