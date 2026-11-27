"""Development spawner: READY handshake, worker token, clean environment, logs, stop and delete."""
from __future__ import annotations

import json
import logging
import ntpath
import sys
import time
import uuid

import httpx
import pytest

IS_WINDOWS = sys.platform == "win32"

from starling_server.config import load_settings
from starling_server.engines.dev_spawner import DevSpawner
from starling_server.engines.spawner import (
    EngineSecrets,
    SpawnError,
    assert_engine_environment_clean,
    new_worker_token,
)
from starling_server.services.tokens import mint_engine_token

from ..conftest import base_env
from .fake_engine import LOG_CANARY
from .support import fake_command, run, wait_until

SECRET_ENV = {
    "AI_API_KEY": "canary-ai-key-value-0001",
    "AI_API_KEY_SECONDARY": "canary-ai-key-value-0002",
    "STRIPE_SECRET_KEY": "canary-stripe-value-0003",
    "STRIPE_WEBHOOK_SECRET": "canary-webhook-value-0004",
    "SMTP_PASSWORD": "canary-smtp-value-0005",
    "STARLING_SERVICES_TOKEN_KEY": "canary-services-key-value-0006",
    "BACKUP_PASSPHRASE": "canary-backup-value-0007",
}


def make_spawner(settings, *args: str, ready_timeout_s: float = 15.0) -> DevSpawner:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return DevSpawner(settings, command=fake_command(*args), cwd=str(settings.data_dir), ready_timeout_s=ready_timeout_s)


def secrets_for(settings, tenant_id: str) -> EngineSecrets:
    return EngineSecrets(new_worker_token(), mint_engine_token(settings.services_token_key, tenant_id, int(time.time())))


def test_spawn_reports_ready_and_only_the_worker_token_opens_the_engine(settings):
    tenant_id = str(uuid.uuid4())
    secrets = secrets_for(settings, tenant_id)

    async def scenario():
        spawner = make_spawner(settings)
        try:
            engine = await spawner.spawn(3, tenant_id, secrets)
            base = "http://" + engine.listen[len("tcp:"):]
            async with httpx.AsyncClient(trust_env=False) as client:
                missing = await client.get(base + "/livez")
                wrong = await client.get(base + "/livez", headers={"Authorization": f"Bearer {new_worker_token()}"})
                right = await client.get(base + "/livez", headers={"Authorization": f"Bearer {secrets.worker_token}"})
                unknown = await client.get(base + "/health", headers={"Authorization": f"Bearer {secrets.worker_token}"})
            return engine, missing, wrong, right, unknown
        finally:
            await spawner.close()

    engine, missing, wrong, right, unknown = run(scenario())
    assert engine.listen.startswith("tcp:127.0.0.1:") and engine.tenant_id == tenant_id
    assert missing.status_code == 404 and missing.content == b""
    assert wrong.status_code == 404 and wrong.content == b""
    assert right.status_code == 200 and right.json() == {"ok": True}  # negative control
    assert unknown.status_code == 404
    received = json.loads((settings.tenants_dir / "3" / "stdin.json").read_text())
    assert received["keys"] == ["services_token", "tenant_id", "worker_token"]
    assert received["worker_token_len"] == 43
    assert received["services_token_subject"] == f"t-{tenant_id}"


def test_engine_environment_is_the_contract_and_never_holds_secrets(settings, monkeypatch):
    for name, value in SECRET_ENV.items():
        monkeypatch.setenv(name, value)
    tenant_id = str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings)
        try:
            await spawner.spawn(4, tenant_id, secrets_for(settings, tenant_id))
        finally:
            await spawner.close()

    run(scenario())
    env = json.loads((settings.tenants_dir / "4" / "env.json").read_text())
    for name, value in SECRET_ENV.items():
        assert name not in env
        assert not any(value in item for item in env.values())
    assert settings.secret_key and not any("STARLING_SECRET_KEY" == name for name in env)
    assert env["STARLING_HOSTED"] == "1"
    assert env["STARLING_APP_DIR"] == str(settings.tenants_dir / "4")
    assert env["STARLING_LISTEN"] == "tcp:127.0.0.1:0"
    assert env["STARLING_SERVICES_URL"] == settings.services_url
    assert (env["STARLING_MODEL_QUALITY"], env["STARLING_MODEL_FAST"], env["STARLING_MODEL_VISION"]) == (
        "starling-quality", "starling-fast", "starling-vision")
    assert env["STARLING_RESEARCH"] == "proxy" and env["STARLING_DISABLE_WARM"] == "1" and env["TZ"] == "UTC"
    assert env["OMP_NUM_THREADS"] == env["OPENBLAS_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "1"


@pytest.mark.skipif(not IS_WINDOWS, reason="only Windows resolves the home directory from USERPROFILE")
def test_the_engine_home_is_resolvable_on_windows(settings):
    """`app.paths` reads `Path.home()` while it imports, and Windows ignores HOME.

    Regression: without USERPROFILE the worker died before READY with "Could not determine
    home directory", so no engine ever started on a Windows development or e2e machine.
    """
    tenant_id = str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings)
        try:
            await spawner.spawn(11, tenant_id, secrets_for(settings, tenant_id))
        finally:
            await spawner.close()

    run(scenario())
    env = json.loads((settings.tenants_dir / "11" / "env.json").read_text())
    app_dir = str(settings.tenants_dir / "11")
    assert env["HOME"] == app_dir
    # Both spellings Windows understands, so `~` resolves inside the customer's own folder
    # whichever one `ntpath.expanduser` reaches for.
    assert _windows_home(env) == app_dir
    assert _windows_home({k: v for k, v in env.items() if k != "USERPROFILE"}) == app_dir


def _windows_home(env: dict[str, str]) -> str:
    """What `ntpath.expanduser` resolves `~` to with this environment (see ntpath.expanduser)."""
    if "USERPROFILE" in env:
        return env["USERPROFILE"]
    assert "HOMEPATH" in env, "neither USERPROFILE nor HOMEPATH: `~` cannot be resolved"
    return ntpath.join(env.get("HOMEDRIVE", ""), env["HOMEPATH"])


def test_secret_guard_refuses_leaked_names_and_values():
    with pytest.raises(SpawnError):
        assert_engine_environment_clean({"AI_API_KEY": "x"}, {})
    with pytest.raises(SpawnError):
        assert_engine_environment_clean({"SOME_PATH": "prefix-canary-ai-key-value-0001"}, {"AI_API_KEY": SECRET_ENV["AI_API_KEY"]})
    assert_engine_environment_clean({"SOME_PATH": "harmless"}, {"AI_API_KEY": SECRET_ENV["AI_API_KEY"]})  # control


def test_ready_timeout_fails_cleanly_and_leaves_no_process(settings):
    tenant_id = str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings, "--no-ready", ready_timeout_s=1.5)
        started = time.monotonic()
        try:
            with pytest.raises(SpawnError, match="READY within"):
                await spawner.spawn(5, tenant_id, secrets_for(settings, tenant_id))
            return time.monotonic() - started, await spawner.is_running(5), await spawner.status()
        finally:
            await spawner.close()

    elapsed, running, status = run(scenario())
    assert 1.4 <= elapsed < 10
    assert running is False and status == []
    assert (settings.tenants_dir / "5" / "marker.json").exists()  # it did start before timing out


def test_exit_before_ready_and_foreign_listen_address_are_refused(settings):
    tenant_id = str(uuid.uuid4())

    async def scenario():
        early = make_spawner(settings, "--exit-before-ready", "3")
        foreign = make_spawner(settings, "--ready-listen", "tcp:10.0.0.5:8080")
        try:
            with pytest.raises(SpawnError, match="code 3 before READY"):
                await early.spawn(6, tenant_id, secrets_for(settings, tenant_id))
            with pytest.raises(SpawnError, match="tcp:127.0.0.1"):
                await foreign.spawn(6, tenant_id, secrets_for(settings, tenant_id))
            return await foreign.is_running(6)
        finally:
            await early.close()
            await foreign.close()

    assert run(scenario()) is False


def test_engine_logs_are_forwarded_without_their_content(settings, caplog):
    caplog.set_level(logging.INFO, logger="starling_server.engines.dev_spawner")
    tenant_id = str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings, "--log-canary")
        try:
            await spawner.spawn(7, tenant_id, secrets_for(settings, tenant_id))
            await __import__("asyncio").to_thread(wait_until, lambda: "draft_finished" in caplog.text, 10)
        finally:
            await spawner.close()

    run(scenario())
    assert "event=draft_finished" in caplog.text  # the line was received and forwarded
    assert LOG_CANARY not in caplog.text


def test_unexpected_exit_is_reported_but_a_stop_is_not(settings):
    reported: list[tuple[int, int | None, str]] = []
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings)
        spawner.set_exit_callback(lambda seq, code, reason: reported.append((seq, code, reason)))
        try:
            secrets = secrets_for(settings, a_id)
            engine = await spawner.spawn(8, a_id, secrets)
            await spawner.spawn(9, b_id, secrets_for(settings, b_id))
            await spawner.stop(9, grace_s=5)
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.HTTPError):
                    await client.post("http://" + engine.listen[4:] + "/browser/assist",
                                      json={"mode": "context", "fake": {"script": "crash"}},
                                      headers={"Authorization": f"Bearer {secrets.worker_token}"})
            for _ in range(100):
                if reported:
                    break
                await __import__("asyncio").sleep(0.05)
            return await spawner.is_running(8)
        finally:
            await spawner.close()

    assert run(scenario()) is False
    assert reported == [(8, 7, "exited")]


def test_stop_and_delete_data_touch_only_that_tenant(settings):
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings)
        try:
            await spawner.spawn(10, a_id, secrets_for(settings, a_id))
            await spawner.spawn(11, b_id, secrets_for(settings, b_id))
            await spawner.stop(10, grace_s=5)
            stopped = await spawner.is_running(10)
            await spawner.delete_data(10)
            return stopped, await spawner.is_running(11)
        finally:
            await spawner.close()

    stopped, other_running = run(scenario())
    assert stopped is False
    assert not (settings.tenants_dir / "10").exists()
    assert other_running is True and (settings.tenants_dir / "11" / "marker.json").exists()


def test_development_spawner_refuses_os_isolation(tmp_path):
    env = base_env(tmp_path / "data")
    env["STARLING_ISOLATION"] = "os"
    with pytest.raises(SpawnError):
        DevSpawner(load_settings(env))
    DevSpawner(load_settings(base_env(tmp_path / "data")))  # control


def test_engines_do_not_inherit_pythonpath_and_deletion_leaves_no_folder_behind(settings, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "injected-path-for-engine-test")
    tenant_id = str(uuid.uuid4())

    async def scenario():
        spawner = make_spawner(settings)
        try:
            await spawner.spawn(12, tenant_id, secrets_for(settings, tenant_id))
            env = json.loads((settings.tenants_dir / "12" / "env.json").read_text())
            await spawner.delete_data(12)
            return env
        finally:
            await spawner.close()

    env = run(scenario())
    assert "PYTHONPATH" not in env and env["PYTHONNOUSERSITE"] == "1"
    assert [path.name for path in settings.tenants_dir.iterdir()] == []  # no ".deleting-*" leftovers
