"""Supervisor: request validation, uid/gid mapping, environments and memory limits run everywhere;
directory modes, setuid spawns and cross-engine isolation need Linux and root (the container)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from starling_server import supervisor
from starling_server.engines.spawner import EngineSecrets, SpawnError, engine_environment, is_secret_env_name, new_worker_token
from starling_server.services.tokens import mint_engine_token

from .support import FAKE_ENGINE, read_jsonl, run

LINUX = sys.platform == "linux"
IS_ROOT = LINUX and os.geteuid() == 0
root_on_linux = pytest.mark.skipif(not IS_ROOT, reason="needs Linux and root (run in the container)")

SECRET_ENV = {
    "STARLING_SECRET_KEY": "canary-session-secret-0001",
    "STARLING_SERVICES_TOKEN_KEY": "canary-services-secret-0002",
    "AI_API_KEY": "canary-ai-secret-0003",
    "AI_API_KEY_SECONDARY": "canary-ai-secret-0004",
    "STRIPE_SECRET_KEY": "canary-stripe-secret-0005",
    "STRIPE_WEBHOOK_SECRET": "canary-webhook-secret-0006",
    "SMTP_PASSWORD": "canary-smtp-secret-0007",
    "BACKUP_PASSPHRASE": "canary-backup-secret-0008",
}


def spawn_request(settings, seq: int = 5, tenant_id: str | None = None, **overrides) -> dict:
    tenant_id = tenant_id or str(uuid.uuid4())
    request = {
        "op": "spawn",
        "tenant_seq": seq,
        "tenant_id": tenant_id,
        "secrets": {
            "worker_token": new_worker_token(),
            "services_token": mint_engine_token(settings.services_token_key, tenant_id, int(time.time())),
        },
    }
    request.update(overrides)
    return request


def parse(settings, payload, **kwargs) -> dict:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return supervisor.parse_request(raw, token_key=settings.services_token_key, now=int(time.time()), **kwargs)


# --------------------------------------------------------------------------- everywhere


def test_valid_requests_are_accepted_and_normalised(settings):
    request = spawn_request(settings)
    parsed = parse(settings, request)
    assert parsed["op"] == "spawn" and parsed["tenant_seq"] == 5 and parsed["tenant_id"] == request["tenant_id"]
    assert parsed["worker_token"] == request["secrets"]["worker_token"]
    assert parse(settings, {"op": "stop", "tenant_seq": 5, "grace_s": 3}) == {"op": "stop", "tenant_seq": 5, "grace_s": 3.0}
    assert parse(settings, {"op": "stop", "tenant_seq": 5})["grace_s"] == 10.0
    assert parse(settings, {"op": "delete_data", "tenant_seq": 10_000_000}) == {"op": "delete_data", "tenant_seq": 10_000_000}
    assert parse(settings, {"op": "status"}) == {"op": "status"}


def _bad_requests(settings):
    tenant = str(uuid.uuid4())
    other = str(uuid.uuid4())
    key = settings.services_token_key
    good = spawn_request(settings, tenant_id=tenant)
    secrets = good["secrets"]
    return {
        "not json": b"not json",
        "array": b"[1]",
        "unknown op": {"op": "launch"},
        "extra field": {"op": "status", "verbose": True},
        "missing tenant": {"op": "stop"},
        "bool seq": {**good, "tenant_seq": True},
        "zero seq": {**good, "tenant_seq": 0},
        "huge seq": {**good, "tenant_seq": 10_000_001},
        "string seq": {**good, "tenant_seq": "5"},
        "float seq": {**good, "tenant_seq": 5.0},
        "bad uuid": {**good, "tenant_id": "not-a-uuid"},
        "upper uuid": {**good, "tenant_id": tenant.upper()},
        "missing secret": {**good, "secrets": {"worker_token": secrets["worker_token"]}},
        "extra secret": {**good, "secrets": {**secrets, "ai_api_key": "x"}},
        "short worker token": {**good, "secrets": {**secrets, "worker_token": secrets["worker_token"][:42]}},
        "worker token alphabet": {**good, "secrets": {**secrets, "worker_token": "+" * 43}},
        "token for another tenant": {**good, "secrets": {**secrets, "services_token": mint_engine_token(key, other, int(time.time()))}},
        "expired token": {**good, "secrets": {**secrets, "services_token": mint_engine_token(key, tenant, int(time.time()) - 90_000)}},
        "token from another key": {**good, "secrets": {**secrets, "services_token": mint_engine_token(b"k" * 32, tenant, int(time.time()))}},
        "garbage token": {**good, "secrets": {**secrets, "services_token": "svc1.x.y"}},
        "negative grace": {"op": "stop", "tenant_seq": 5, "grace_s": -1},
        "long grace": {"op": "stop", "tenant_seq": 5, "grace_s": 61},
        "bool grace": {"op": "stop", "tenant_seq": 5, "grace_s": True},
        "oversized": b'{"op": "status", "pad": "' + b"x" * (supervisor.MAX_REQUEST_BYTES + 1) + b'"}',
    }


def test_malformed_requests_are_refused(settings):
    for name, payload in _bad_requests(settings).items():
        with pytest.raises(supervisor.RequestError):
            parse(settings, payload)
            pytest.fail(f"accepted: {name}")


def test_services_token_needs_the_key_unless_explicitly_structural(settings):
    request = spawn_request(settings)
    raw = json.dumps(request).encode()
    with pytest.raises(supervisor.RequestError):
        supervisor.parse_request(raw, token_key=None, now=int(time.time()))
    assert supervisor.parse_request(raw, token_key=None, now=int(time.time()), require_token_key=False)["op"] == "spawn"
    wrong_subject = spawn_request(settings)
    wrong_subject["secrets"]["services_token"] = mint_engine_token(settings.services_token_key, str(uuid.uuid4()), int(time.time()))
    with pytest.raises(supervisor.RequestError):
        supervisor.parse_request(json.dumps(wrong_subject).encode(), token_key=None, now=int(time.time()), require_token_key=False)


def test_engine_ids_map_each_tenant_to_its_own_uid_and_gid():
    assert supervisor.engine_ids(1, 200_000) == (200_001, 200_001)
    assert supervisor.engine_ids(10_000_000, 200_000) == (10_200_000, 10_200_000)
    assert len({supervisor.engine_ids(seq, 200_000)[0] for seq in range(1, 500)}) == 499
    reserved = {0, supervisor.GATEWAY_UID, supervisor.SERVICES_UID, supervisor.ENGINE_GROUP_GID, supervisor.DB_GROUP_GID}
    assert not reserved & {supervisor.engine_ids(seq, supervisor.MIN_ENGINE_UID_BASE)[0] for seq in (1, 2, 9_999)}
    for seq, base in ((0, 200_000), (5, 1_000), (5, True)):
        with pytest.raises(ValueError):
            supervisor.engine_ids(seq, base)


def test_engine_environment_is_the_contract_only(settings, tmp_path):
    layout = supervisor.Layout(tmp_path / "data", tmp_path / "run")
    env = supervisor.engine_env(settings, layout, 7)
    contract = engine_environment(app_dir=layout.tenant_dir(7), listen="unix:" + str(layout.engine_socket(7)),
                                  services_url=settings.services_url)
    assert set(env) == set(contract) | {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
    assert not any(is_secret_env_name(name) for name in env)
    assert env["STARLING_LISTEN"] == "unix:" + str(layout.engine_socket(7))
    assert env["STARLING_APP_DIR"] == str(layout.tenant_dir(7))
    supervisor.assert_engine_environment_clean(env, SECRET_ENV)
    with pytest.raises(SpawnError):  # negative control
        supervisor.assert_engine_environment_clean({**env, "HOME": SECRET_ENV["AI_API_KEY"]}, SECRET_ENV)


def test_gateway_and_services_receive_only_their_own_secrets(settings):
    environ = {**SECRET_ENV, "STARLING_ENV": "production", "PATH": "/usr/bin"}
    gateway = supervisor.gateway_environment(environ)
    services = supervisor.services_environment(environ)
    assert not {"AI_API_KEY", "AI_API_KEY_SECONDARY", "BACKUP_PASSPHRASE"} & set(gateway)
    assert {"STARLING_SECRET_KEY", "STARLING_SERVICES_TOKEN_KEY", "STRIPE_SECRET_KEY", "SMTP_PASSWORD"} <= set(gateway)
    assert not {"STARLING_SECRET_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "SMTP_PASSWORD", "BACKUP_PASSPHRASE"} & set(services)
    assert {"AI_API_KEY", "AI_API_KEY_SECONDARY", "STARLING_SERVICES_TOKEN_KEY"} <= set(services)
    argv = supervisor.gateway_command(settings)
    assert argv[argv.index("--workers") + 1] == "1" and "--factory" in argv and "starling_server.app:create_app" in argv


def test_handle_line_never_raises_and_never_echoes_secrets(settings, tmp_path):
    sup = supervisor.Supervisor(settings, layout=supervisor.Layout(tmp_path / "data", tmp_path / "run"),
                                run_gateway=False, run_services=False)
    bad = spawn_request(settings, seq=0)
    response = run(sup.handle_line(json.dumps(bad).encode()))
    assert response["ok"] is False
    text = json.dumps(response)
    assert bad["secrets"]["worker_token"] not in text and bad["secrets"]["services_token"] not in text
    assert run(sup.handle_line(b'{"op":"status"}')) == {"ok": True, "engines": []}


def test_memory_limits_flag_soft_and_kill_hard(settings, tmp_path, monkeypatch):
    sup = supervisor.Supervisor(settings, layout=supervisor.Layout(tmp_path / "data", tmp_path / "run"),
                                run_gateway=False, run_services=False)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)  # absent on Windows
    usage = {101: settings.engine_memory_soft_mb + 1, 102: settings.engine_memory_hard_mb + 1, 103: 10}
    monkeypatch.setattr(supervisor, "read_rss_mb", lambda pid: usage[pid])

    class Proc:
        def __init__(self, pid):
            self.pid, self.returncode = pid, None

    for seq, pid in ((1, 101), (2, 102), (3, 103)):
        sup.engines[seq] = supervisor.EngineProcess(seq, str(uuid.uuid4()), 200_000 + seq, Proc(pid), 0, listen="unix:x")
    run(sup.check_memory())
    flags = {item["tenant_seq"]: item["over_soft_limit"] for item in sup.status()["engines"]}
    assert flags == {1: True, 2: False, 3: False}
    assert [pid for pid, _ in killed] == [102]


def test_gateway_runs_without_uvicorns_access_log(settings):
    assert "--no-access-log" in supervisor.gateway_command(settings)


def test_maintenance_flag_values():
    assert supervisor.maintenance_requested({"STARLING_MAINTENANCE": "1"})
    assert supervisor.maintenance_requested({"STARLING_MAINTENANCE": " TRUE "})
    for value in ("", "0", "false", "no"):
        assert not supervisor.maintenance_requested({"STARLING_MAINTENANCE": value})
    assert not supervisor.maintenance_requested({})


def test_maintenance_mode_starts_nothing_touches_no_data_and_stays_alive(settings, tmp_path, monkeypatch, caplog):
    import logging

    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance mode must not start or prepare anything")

    monkeypatch.setattr(supervisor, "start_backup_thread", forbidden)
    monkeypatch.setattr(supervisor, "prepare_base_dirs", forbidden)
    caplog.set_level(logging.INFO, logger="starling_server.supervisor")
    layout = supervisor.Layout(tmp_path / "data", tmp_path / "run")
    sup = supervisor.Supervisor(settings, layout=layout, environ={"STARLING_MAINTENANCE": "1"})

    async def scenario():
        task = asyncio.create_task(sup.run())
        await asyncio.sleep(0.5)
        alive = not task.done()
        sup.request_stop()
        return alive, await asyncio.wait_for(task, 5)

    alive, code = run(scenario())
    assert alive is True and code == 0
    assert not layout.data_dir.exists() and not layout.run_dir.exists()
    assert sup.engines == {} and sup._children == [] and sup._backup is None
    assert len([r for r in caplog.records if "maintenance mode" in r.getMessage()]) == 1
    with pytest.raises(RuntimeError):
        run(sup.start())


def test_main_in_maintenance_mode_waits_without_loading_settings(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance must not need settings or start a supervisor")

    waited: list[bool] = []

    async def fake_wait(stop=None):
        waited.append(True)

    monkeypatch.setattr(supervisor, "load_settings", forbidden)
    monkeypatch.setattr(supervisor, "Supervisor", forbidden)
    monkeypatch.setattr(supervisor, "wait_for_termination", fake_wait)
    monkeypatch.setenv("STARLING_MAINTENANCE", "1")
    assert supervisor.main([]) == 0 and waited == [True]
    monkeypatch.setenv("STARLING_MAINTENANCE", "0")
    with pytest.raises(AssertionError):  # control: without the flag the settings are loaded
        supervisor.main([])


def test_backups_start_outside_maintenance_and_stop_on_shutdown(settings, tmp_path, monkeypatch):
    import threading

    stop_event = threading.Event()
    thread = threading.Thread(target=stop_event.wait, name="fake-backup", daemon=True)
    calls = []

    def fake_start(given_settings):
        calls.append(given_settings)
        thread.start()
        return thread, stop_event

    async def no_socket(self):
        return None

    monkeypatch.setattr(supervisor, "start_backup_thread", fake_start)
    monkeypatch.setattr(supervisor, "prepare_base_dirs", lambda layout: None)
    monkeypatch.setattr(supervisor.Supervisor, "_open_socket", no_socket)
    sup = supervisor.Supervisor(settings, layout=supervisor.Layout(tmp_path / "data", tmp_path / "run"),
                                run_gateway=False, run_services=False, environ={})

    async def scenario():
        await sup.start()
        running = thread.is_alive()
        await sup.stop()
        return running

    assert run(scenario()) is True
    assert calls == [settings] and stop_event.is_set() and not thread.is_alive() and sup._backup is None


def test_deletion_renames_to_a_non_numeric_name_first_and_leftovers_are_swept(tmp_path, monkeypatch):
    layout = supervisor.Layout(tmp_path / "data", tmp_path / "run")
    for seq in (7, 8):
        layout.tenant_dir(seq).mkdir(parents=True)
        (layout.tenant_dir(seq) / "data.bin").write_bytes(b"customer data")
    layout.engine_socket(7).parent.mkdir(parents=True)
    real_rmtree = supervisor.shutil.rmtree

    def crash(path, *args, **kwargs):
        raise OSError("simulated crash during deletion")

    monkeypatch.setattr(supervisor.shutil, "rmtree", crash)
    with pytest.raises(OSError):
        supervisor.remove_tenant_data(layout, 7)
    monkeypatch.setattr(supervisor.shutil, "rmtree", real_rmtree)
    names = sorted(path.name for path in layout.tenants_dir.iterdir())
    assert "7" not in names and "8" in names  # a backup no longer sees a numeric folder for tenant 7
    assert any(name.startswith(".deleting-7-") for name in names)
    assert supervisor.sweep_deleting_dirs(layout) == 1
    assert sorted(path.name for path in layout.tenants_dir.iterdir()) == ["8"]
    supervisor.remove_tenant_data(layout, 8)  # control: a normal deletion leaves nothing behind
    assert list(layout.tenants_dir.iterdir()) == []


def test_engine_environment_never_inherits_pythonpath(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/app/image/pythonpath")
    env = supervisor.engine_env(settings, supervisor.Layout(tmp_path / "data", tmp_path / "run"), 3)
    assert "PYTHONPATH" not in env and env["PYTHONNOUSERSITE"] == "1"


# --------------------------------------------------------------------------- Linux, root


@root_on_linux
def test_tenants_folder_owner_and_mode_are_reapplied_at_every_start(tmp_path):
    layout = supervisor.Layout(tmp_path / "data", tmp_path / "run")
    supervisor.prepare_base_dirs(layout)
    os.chown(layout.tenants_dir, supervisor.GATEWAY_UID, supervisor.GATEWAY_GID)
    os.chmod(layout.tenants_dir, 0o755)
    supervisor.prepare_base_dirs(layout)
    info = os.stat(layout.tenants_dir)
    assert (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) == (0o711, 0, 0)


def _make_traversable(path: Path) -> None:
    for parent in [path, *path.parents]:
        if str(parent) in ("/", "/tmp"):
            break
        os.chmod(parent, os.stat(parent).st_mode | 0o711)


@root_on_linux
def test_directories_have_the_contract_modes_and_owners(tmp_path):
    layout = supervisor.Layout(tmp_path / "data", tmp_path / "run")
    supervisor.prepare_base_dirs(layout)

    def check(path, mode, uid, gid):
        info = os.stat(path)
        assert (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) == (mode, uid, gid), path

    check(layout.control_dir, 0o2770, supervisor.GATEWAY_UID, supervisor.DB_GROUP_GID)
    check(layout.control_dir / "control.db", 0o660, supervisor.GATEWAY_UID, supervisor.DB_GROUP_GID)
    check(layout.tenants_dir, 0o711, 0, 0)
    check(layout.spool_dir, 0o700, supervisor.GATEWAY_UID, supervisor.GATEWAY_GID)
    check(layout.engines_run_dir, 0o711, 0, 0)
    supervisor.prepare_tenant_dirs(layout, 7, 200_007, 200_007)
    check(layout.tenant_dir(7), 0o700, 200_007, 200_007)
    check(layout.engine_socket(7).parent, 0o2750, 200_007, supervisor.GATEWAY_GID)
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, layout.tenant_dir(8))
    with pytest.raises(OSError):
        supervisor.prepare_tenant_dirs(layout, 8, 200_008, 200_008)
    assert os.stat(target).st_uid == 0  # the link target was not handed to the engine user
    supervisor.remove_tenant_data(layout, 7)
    assert not layout.tenant_dir(7).exists()


@root_on_linux
def test_engines_run_as_their_own_user_without_secrets_and_cannot_reach_each_other(settings, tmp_path):
    from starling_server.engines.supervisor_client import SupervisorClient

    _make_traversable(tmp_path)
    layout = supervisor.Layout(settings.data_dir, settings.run_dir)
    environ = {**os.environ, **SECRET_ENV}
    ids = {7: str(uuid.uuid4()), 8: str(uuid.uuid4())}
    tokens = {seq: new_worker_token() for seq in ids}

    async def scenario():
        sup = supervisor.Supervisor(settings, layout=layout, engine_command=[sys.executable, str(FAKE_ENGINE)],
                                    engine_cwd=Path("/"), allowed_peer_uids=(0,), run_gateway=False,
                                    run_services=False, environ=environ)
        await sup.start()
        client = SupervisorClient(settings, socket_path=str(layout.socket_path))
        try:
            handles = {}
            for seq, tenant_id in ids.items():
                token = mint_engine_token(settings.services_token_key, tenant_id, int(time.time()))
                handles[seq] = await client.spawn(seq, tenant_id, EngineSecrets(tokens[seq], token))
            base_url, transport = client.transport_for(handles[7].listen)
            async with httpx.AsyncClient(base_url=base_url, transport=transport, trust_env=False,
                                         headers={"Authorization": f"Bearer {tokens[7]}"}) as http:
                livez = (await http.get("/livez")).status_code
            status = await client.status()
            refused = await sup.handle_line(json.dumps(spawn_request(settings, seq=7, tenant_id=ids[7])).encode())
            return livez, status, refused
        finally:
            await sup.stop()

    loop_result = asyncio.run(scenario())
    livez, status, refused = loop_result
    assert livez == 200 and {item.tenant_seq for item in status} == {7, 8}
    assert refused["ok"] is False  # already running
    identity = json.loads((layout.tenant_dir(7) / "identity.json").read_text())
    assert (identity["uid"], identity["gid"]) == (200_007, 200_007)
    assert identity["groups"] == [supervisor.ENGINE_GROUP_GID] and identity["umask"] == 0o007
    assert identity["no_new_privs"] == 1
    env = json.loads((layout.tenant_dir(7) / "env.json").read_text())
    for name, value in SECRET_ENV.items():
        assert name not in env and not any(value in item for item in env.values())

    probe = "import sys; open(sys.argv[1], 'rb').read()"
    other = subprocess.run([sys.executable, "-c", probe, str(layout.tenant_dir(7) / "marker.json")],
                           user=200_008, group=200_008, extra_groups=[supervisor.ENGINE_GROUP_GID], capture_output=True)
    same = subprocess.run([sys.executable, "-c", probe, str(layout.tenant_dir(7) / "marker.json")],
                          user=200_007, group=200_007, extra_groups=[supervisor.ENGINE_GROUP_GID], capture_output=True)
    assert other.returncode != 0 and b"PermissionError" in other.stderr
    assert same.returncode == 0  # control
    connect = ("import socket, sys; s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])")
    socket_probe = subprocess.run([sys.executable, "-c", connect, str(layout.engine_socket(8))],
                                  user=200_007, group=200_007, extra_groups=[supervisor.ENGINE_GROUP_GID], capture_output=True)
    assert socket_probe.returncode != 0 and b"PermissionError" in socket_probe.stderr
    assert read_jsonl(layout.tenant_dir(8) / "served.jsonl") == []
