"""The engine's data folder (API-native pass, 2026-09-22): STARLING_APP_DIR, else the dev folder."""
from __future__ import annotations

from pathlib import Path

from app import paths


def test_the_supervisor_folder_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("STARLING_APP_DIR", str(tmp_path / "tenant-7"))
    assert paths.app_dir() == (tmp_path / "tenant-7").resolve()


def test_a_relative_folder_is_never_used(monkeypatch):
    monkeypatch.setenv("STARLING_APP_DIR", "relative/data")
    assert paths.app_dir() == paths.DEV_DATA_DIR


def test_without_the_variable_a_development_run_stays_in_the_repository(monkeypatch):
    monkeypatch.delenv("STARLING_APP_DIR", raising=False)
    folder = paths.app_dir()
    assert folder == paths.DEV_DATA_DIR and folder.parts[-3:] == ("backend", "engine", ".data")
    # Inside this checkout, wherever the checkout is (someone may unpack it under AppData): never the
    # old desktop product's per-user folder.
    assert folder.parent == Path(paths.__file__).resolve().parents[1], "never a desktop folder"
