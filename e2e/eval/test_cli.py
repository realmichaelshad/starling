"""The evaluation's guards: a real run needs an explicit spend cap and a key, and says so before anything starts.

    backend\\.venv\\Scripts\\python.exe -m pytest e2e/eval -q

Nothing here starts the stack or reaches a provider: every case stops in argument handling.
"""
from __future__ import annotations

import pytest

from e2e.eval import __main__ as cli


def test_the_key_comes_from_the_environment_first_then_the_file(tmp_path, monkeypatch):
    key_file = tmp_path / ".env.eval"
    key_file.write_text("# a comment\nAI_API_KEY='fake-key-from-the-file-1234'\n", encoding="utf-8")
    monkeypatch.delenv(cli.KEY_ENV, raising=False)
    assert cli.read_key(key_file) == "fake-key-from-the-file-1234"
    monkeypatch.setenv(cli.KEY_ENV, "fake-key-from-the-environment-5678")
    assert cli.read_key(key_file) == "fake-key-from-the-environment-5678"
    monkeypatch.delenv(cli.KEY_ENV)
    assert cli.read_key(tmp_path / "missing.env") == ""


@pytest.mark.parametrize("argv", [
    ["--real"],                                  # no cap at all
    ["--real", "--spend-up-to", "0.10"],         # below the smallest cap
    ["--real", "--spend-up-to", "100"],          # above the largest
    ["--personas", "terse,nobody"],              # a customer the tool does not know
    ["--real", "--spend-up-to", "3", "--golden"],  # golden answers replace the provider's: never with --real
])
def test_a_run_that_could_surprise_someone_is_refused_before_it_starts(argv, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda *a, **k: pytest.fail("the stack must not start"))
    with pytest.raises(SystemExit) as refused:
        cli.main(argv)
    assert refused.value.code == 2


def test_a_real_run_without_a_key_says_where_to_put_one(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(cli.KEY_ENV, raising=False)
    monkeypatch.setattr(cli, "run", lambda *a, **k: pytest.fail("the stack must not start"))
    with pytest.raises(SystemExit):
        cli.main(["--real", "--spend-up-to", "3", "--key-file", str(tmp_path / "none.env")])
    assert cli.KEY_ENV in capsys.readouterr().err
