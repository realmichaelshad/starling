from __future__ import annotations

import pytest

from app import cloud, main, providers, security, storage, strategy_v2
from app.account_model import reset_topic_profile_cache
from app.intelligence import reset_personal_model_cache


@pytest.fixture(autouse=True)
def isolated_local_state(tmp_path, monkeypatch):
    app_dir = tmp_path / "Starling"
    monkeypatch.setattr(security, "APP_DIR", app_dir)
    # The bearer token is CACHED in the module. Without clearing the cache a
    # token minted under one test's temp directory would authenticate every
    # later test against a file that no longer exists.
    monkeypatch.setattr(security, "TOKEN_PATH", app_dir / "starling.token")
    monkeypatch.setattr(security, "_token_cache", None)
    monkeypatch.setattr(security, "_worker_token", None)
    monkeypatch.setattr(storage, "DB_PATH", app_dir / "starling.db")
    # Every per-customer path resolves through this variable.
    monkeypatch.setenv("STARLING_APP_DIR", str(app_dir))
    # Tests never reach a network service: no services URL or token, web
    # research off, no warm loop, and never the hosted flag of a real worker.
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    for name in ("STARLING_HOSTED", "STARLING_SERVICES_URL", "STARLING_LISTEN",
                 "STARLING_MODEL_QUALITY", "STARLING_MODEL_FAST", "STARLING_MODEL_VISION",
                 # API-native pass: a test never starts the background jobs by accident.
                 "STARLING_BACKGROUND_JOBS"):
        monkeypatch.delenv(name, raising=False)
    reset_personal_model_cache()
    reset_topic_profile_cache()
    # The approach detector and dimension grouping cache per corpus_version;
    # one test's shifted account or beat map must not leak into the next.
    from app.account_model import reset_topic_dimension_cache
    from app.approach import reset_approach_cache
    reset_approach_cache()
    reset_topic_dimension_cache()
    # strategy_v2 caches the parsed manual in a module global; reset it per test
    # so no test scores against another test's manual.
    strategy_v2.reload_strategy()
    # The shared router and the services client are process-global too.
    providers.reset_router()
    cloud.reset()
    # The observed-topic pool is cached in a module global; one test's
    # candidates must not decide another test's post topic.
    main.reset_topic_cache()
    # `_DRAFTED_THIS_SESSION` is the in-process repeat corpus: anything that
    # generates or remembers a draft appends to it, so a draft from one test
    # would otherwise be visible to every test after it. Cleared in place,
    # because other modules hold a reference to the same list object.
    from app.intelligence import _DRAFTED_THIS_SESSION
    _DRAFTED_THIS_SESSION.clear()
    # The page-read cache (S4) is process-global: one test's page must not be
    # another test's page.
    from app import research
    research.reset_page_cache()
    # The writer block, the voice card and the niche read are cached per database (API-native
    # pass); cleared anyway so no test ever reads another's.
    from app import llm, niche, voice_card
    llm.reset_writer_cache()
    voice_card.invalidate()
    niche._CACHE.update(key=None, read=None, at=0.0)
    storage.initialize()
    yield
    _DRAFTED_THIS_SESSION.clear()
    providers.reset_router()
    cloud.reset()
