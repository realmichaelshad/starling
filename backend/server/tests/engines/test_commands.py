"""Operator commands: `engines-status` reads the registry; `engines-stop` is applied by the pool."""
from __future__ import annotations

import asyncio
import json

import pytest

from starling_server import cli
from starling_server.config import get_settings

from .support import insert_account, make_pool, run


@pytest.fixture
def cli_env(env, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_status_lists_engines_and_stop_is_applied_by_the_running_pool(settings, database, cli_env, capsys):
    a = insert_account(database, settings, 1)

    async def scenario():
        pool = make_pool(settings, database, tick_s=0.1)
        await pool.start()
        try:
            await pool.ensure(a, 1)
            status_code = await asyncio.to_thread(cli.main, ["engines-status", "--json"])
            status_output = capsys.readouterr().out
            stop_code = await asyncio.to_thread(cli.main, ["engines-stop", "--account", a, "--wait", "15"])
            stop_output = capsys.readouterr().out
            return status_code, status_output, stop_code, stop_output, pool.is_live(a)
        finally:
            await pool.close()

    status_code, status_output, stop_code, stop_output, live = run(scenario())
    listed = json.loads(status_output)
    assert status_code == 0 and listed["supervisor"] is None
    assert [(row["tenant_seq"], row["state"]) for row in listed["engines"]] == [(1, "running")]
    assert stop_code == 0 and "engine 1 stopped" in stop_output
    assert live is False


def test_stop_reports_bad_ids_idle_accounts_and_an_absent_gateway(settings, database, cli_env, capsys):
    a = insert_account(database, settings, 1)
    assert cli.main(["engines-stop", "--account", "not-an-id"]) == 2
    assert cli.main(["engines-stop", "--account", a]) == 0
    assert "no running engine" in capsys.readouterr().out
    database.write(lambda conn: conn.execute(
        "INSERT INTO engines (account_id, tenant_seq, pid, listen, state, started_at, last_used_at, restarts) "
        "VALUES (?, 1, 4242, 'tcp:127.0.0.1:9', 'running', 0, 0, 0)", (a,)))
    assert cli.main(["engines-stop", "--account", a, "--wait", "0.3"]) == 3  # nobody applies it
    with database.read() as conn:
        assert conn.execute("SELECT state FROM engines WHERE account_id = ?", (a,)).fetchone()[0] == "stop_requested"
