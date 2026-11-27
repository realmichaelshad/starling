"""Identity is constant in the hosted engine.

The desktop product resolved a repo-root config file for its browser-extension id,
native host name, update channel and frozen-binary paths. None of those exist for a
hosted engine: the gateway owns extension ids, and there is no native host, updater
or frozen build. What remains is the product name and the engine version.
"""
from __future__ import annotations

import inspect
import re

import app
from app import identity, worker


def test_identity_is_the_product_name_and_the_engine_version():
    assert identity.PRODUCT_NAME == "Starling"
    assert identity.ENGINE_VERSION == app.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", identity.ENGINE_VERSION)


def test_the_ready_line_reports_the_identity_version():
    assert worker.engine_version() == identity.ENGINE_VERSION


def test_identity_reads_nothing_and_describes_no_desktop_install():
    source = inspect.getsource(identity)
    for marker in ("starling.config.json", "STARLING_CONFIG", "read_text", "open(",
                   "os.environ", "chrome-extension", "_MEIPASS", '"frozen"', "__compiled__"):
        assert marker not in source, f"identity still carries {marker!r}"
    for gone in ("extension_ids", "extension_origins", "extension_origin_regex",
                 "native_host_name", "update_settings", "product_version",
                 "config_source", "feature"):
        assert not hasattr(identity, gone), f"identity.{gone} should be gone"


def test_the_handle_helpers_stay():
    assert identity.normalize_handle("  @Example_Builder ") == "example_builder"
    assert identity.normalize_handle("@ name") == "name"
    assert identity.normalize_handle(None) == ""
