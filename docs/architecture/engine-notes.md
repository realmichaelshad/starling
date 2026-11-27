# Engine conversion notes (hosted worker, phase 1)

What changed in `backend/engine` so the engine runs as the hosted per-customer worker described in
CONTRACTS §1-4, §6.2 and §13, why, and what is still open. The companion document
`news-pool-shape.md` records the world-service response shapes the engine reads.

---

## 1. How the engine runs now

- **Entry point.** `python -m app.worker`, working directory `backend/engine` (`app/worker.py`).
  1. Exactly one JSON line on stdin, then EOF: `worker_token` (43 base64url characters),
     `services_token`, `tenant_id` (canonical lowercase UUID). The services token must have the
     CONTRACTS §4.3 shape, name this tenant (`t-<tenant_id>`) and not be expired. The engine never
     holds the signing key, so it checks shape, tenant and expiry but not the signature.
  2. Both tokens are kept in module memory only (`security.set_worker_token`,
     `cloud.set_services_token`), never in `os.environ` or on disk.
  3. The `STARLING_LISTEN` listener is bound before uvicorn starts (`unix:` socket chmod 0660, or
     `tcp:127.0.0.1:<port>`; port 0 reports the real port).
  4. `READY {"listen", "pid", "engine_version"}` is written once the app has started. After it,
     stdout carries JSON lines only: stray `print` output is wrapped into a JSON record and cut
     at 300 characters, uvicorn's access log is off, and there is no server or date header.
  5. SIGTERM (CTRL_BREAK on Windows) cancels every in-flight request, lets uvicorn drain for at
     most 10 s, runs `PRAGMA wal_checkpoint(TRUNCATE)` and exits 0. uvicorn's
     `capture_signals` is overridden, because re-raising the signal after shutdown ended the
     process with the signal's own status.
  6. Exit codes: 0 graceful stop; 2 refused to start (bad stdin or `STARLING_LISTEN`; one JSON
     line on stderr, no READY); 3 application startup failed; 4 not started within 30 s.
  7. On POSIX the worker sets `umask 007` (CONTRACTS §2.1).
- **Routes.** Exactly the CONTRACTS §3.3 table (13 routes). There is no `/health`, bootstrap,
  update, provider, local-model, browser-engine, `/import`, `DELETE /data`, CORS, websocket,
  OpenAPI schema or docs. `AuthGate` default-denies with 404 before anything else runs.
  Middleware order: AuthGate, then BodyLimit, then RequestContextMiddleware.
- **Request context** (`app/request_context.py`). `X-Starling-Request-Id` and
  `X-Starling-Time-Zone` are bound in a `contextvars` state per request.
  - Every thread the pipeline starts is started with `contextvars.copy_context().run`, so
    services calls made from threads carry the same request id.
  - `POST /internal/requests/{request_id}/cancel` sets that request's cancel event.
  - `deadline.expired()` is also true after a cancel or a fatal AI refusal, so no further AI call
    starts.
- **Budgets** (`app/deadline.py`). Non-stream request 145 s. Stream 225 s, inside the gateway's
  240 s (CONTRACTS §6.4). First token 85 s, inside about 90 s.
- **Stream events.** `status`, `token`, then exactly one terminal event:
  - `final` for usable, gated, empty and cancelled results;
  - `error` for a fatal AI failure, carrying the services code (for example
    `fair_use_exceeded`, `reservation_inactive`, `call_cap_exceeded`, `budget_exhausted`,
    `ai_rate_limited`, with `retry_after_s` when known);
  - `error` with `bad_request` for an unsupported request, `engine_error` for an unexpected
    failure, and `ai_unavailable` when no first token arrives in time.

  A non-stream result that is unusable because of a refused AI call carries `ok: false`, `error`
  and `error_code`.
- **Time zone.** In hosted mode the machine time zone is never read or synced. The effective
  zone is the stored profile zone when it is not UTC, otherwise the request's
  `X-Starling-Time-Zone`, otherwise none.
- **Background work.** The warm loop never runs in hosted mode. The learning reconciliation runs
  once after READY in an empty context, so its AI calls use `X-Starling-Bucket: background`.

## 2. Services client (`app/cloud.py`, `app/providers.py`, `app/vision.py`)

- Configuration comes from `STARLING_SERVICES_URL` (`unix:/path` uses an httpx UDS transport with
  base `http://starling-services`; `http(s)://` is used as is) and
  `STARLING_MODEL_QUALITY` / `_FAST` / `_VISION` (defaults `starling-quality`, `starling-fast`,
  `starling-vision`). `trust_env=False`, so proxy variables are ignored.
- Headers: `Authorization: Bearer <services_token>`, plus `X-Starling-Request-Id` inside a
  request or `X-Starling-Bucket: background` outside one.
- Payload (`build_payload`):
  - Only the CONTRACTS §4.1 fields are sent: `model`, `messages`, `max_tokens` (from the
    prompt's `num_predict`), `stream`, `response_format` (`json_object` when JSON is requested),
    and `temperature` / `top_p` as hints.
  - System messages are collapsed into one.
  - Message parts are rebuilt to the exact shapes the proxy accepts: `{"type": "text", "text"}`
    and `{"type": "image_url", "image_url": {"url": "data:image/...", "detail"}}`. Image parts
    are only allowed in user messages.
- **`stream_options` is not sent.** The brief asked the engine to request `include_usage`.
  CONTRACTS §4.1 does not list `stream_options` among accepted fields, and the services proxy's
  field allow-list rejects it with 400. The proxy adds `stream_options.include_usage=true`
  upstream itself. The engine still parses the final usage chunk into the call's timing record.
- Errors map to `ServicesError(code, message, retry_after_s)` from the
  `{"error": {"code", "message"}}` body and `Retry-After`.
  - `bad_request` and `input_too_large` are non-fatal: that one call fails and the pipeline may
    continue.
  - Every other code is fatal for the request and ends it with that code.
  - A transport failure is `ai_unavailable`.
- `ProviderRouter` is cloud-only. It keeps a per-call `last_timing` in a `ContextVar`, so
  concurrent requests never read each other's timing. `embed()` returns None; retrieval uses the
  in-process embedder (`app/embeddings.py`) or TF-IDF.
- **Vision.** Images go to the `starling-vision` lane as data URIs with `detail: low`, 5 MB
  maximum, type sniffed. A remote https image is first converted through `POST /v1/image`.

## 3. Web data (`app/research.py`, `app/news.py`, `app/radar.py`, `app/release_radar.py`)

- `STARLING_RESEARCH` is `proxy` (the default) or off (`off`, `0`, `false`, `no`, `none`). Off
  stops every world-service request. Pools already cached are still served.
- **General web search does not exist in the hosted engine.**
  - `research.search_web` returns `[]`.
  - Free-text lookups (`main._web_lookup_answer`) return nothing rather than invent an answer.
  - `research.research()` answers only for the authored query ids in
    `research.AUTHORED_QUERIES`, through `POST /v1/summary {"query_id"}`, and caches the result
    for 45 minutes.
- Page reads go through `POST /v1/fetch`. The text is cut at 6,000 characters on a block
  boundary, and pages under 100 characters count as unreadable. Private and non-http(s) targets
  are refused before the service is asked.
- News comes from `GET /v1/news/headlines?limit=150`:
  - 15-minute per-customer cache, with a 120-second floor on forced refreshes;
  - titles de-duplicated, sources interleaved;
  - per-source health merged, not replaced;
  - an empty refresh serves the stale pool and records a safety event.

  Nothing derived from the account is sent. The radar filters the pool locally.
- The radar's research queries are `research.AUTHORED_QUERY_TEXTS`. Release checks use
  `release_radar.AUTHORED_RELEASE_SUBJECTS`, six fixed "latest model release" subjects ordered by
  local topic pressure.
- `app/registry_sources.py` keeps only the pure shaping of registry and host-filtered story
  records. It makes no requests and returns `[]` without caller-supplied records.
- `app/images.py`: licensed-image search and image download are disabled (`_openverse_results`
  returns `[]`, `_download_licensed_image` returns None). Ranking and rights logic are kept.
  `/v1/fetch` returns no preview image, so the article preview image tier is also empty.

## 4. Other decisions

- **Candidate variety without temperature.** The AI service may ignore sampling, so
  `llm._sampling_for` returns the same hint on every attempt.
  - Each retry gets `llm.variation_note(attempt, earlier)`: it quotes how earlier candidates
    opened, asks for a different opening, sentence shape and angle, and rotates through four
    named opening moves.
  - Reply retries keep their existing retry note, with the variation appended.
  - `_merge_ranked` drops a candidate whose normalised text repeats an earlier one.
  - Tests: `tests/test_candidate_variety.py`.
- **Personal data.**
  - `app/data/strategy_v2.txt` was rewritten as neutral craft guidance: 43 sections, no owner, no
    exemplar posts. Its pin is `EXPECTED_SHA256 = 83251a70...36a` in `app/strategy_v2.py` and
    `app/strategy_source.py`.
  - The per-install manual override and packed data files are gone.
  - The original author's names and handles were removed from `app/` and `tests/`; tests use
    synthetic identities such as `example_builder`.
  - Tests calibrated on the original author's post archive were deleted (section 7). That
    archive is personal data and is not shipped.
- **Vocabulary.** The brand name of the former local model runtime was removed from the entity
  and official-handle lists, because the release gate rejects it. That one product name is no
  longer recognised as a notable subject.
- **Renames.** The prompt and client module formerly named after the local runtime is now
  `app/llm.py`. Its test file is now `tests/test_llm_enhancement.py`.
- **Security.**
  - `app/security.py` has no credential store, cloud keys or permission hardening.
  - In hosted mode the worker token lives only in memory, and without one every route is denied.
  - The token file is still used for development runs outside the worker.
  - `app/storage.py` lost `harden_db`: file modes come from `umask 007` and supervisor-owned
    directories (CONTRACTS §2.1). It gained `checkpoint()`.
- **Logging.** No `logging` calls in `app/` write content. The one `print` that named X handles
  (the profile guard) no longer does.
- **Capabilities.** The "why is it slow / which model" answer is a fixed hosted explanation. It
  makes no AI call.
- **Identity** (`app/identity.py`). Only constants remain: `PRODUCT_NAME = "Starling"` and
  `ENGINE_VERSION` (the package version, reported in READY), plus the handle helpers. The
  repo-root config lookup is gone: extension ids, native host name, update channel,
  frozen-binary paths and feature flags. The gateway owns extension ids
  (`STARLING_EXTENSION_IDS`). Tests: `tests/test_identity.py`.
- **Embeddings without a model runtime.** `models/starling-embedding.onnx` is a single MatMul
  with a seeded 256x96 projection.
  - The matrix ships as `models/starling-embedding.npy`.
  - `app/embeddings.py` computes `features @ W` with numpy and L2-normalises the result.
    `onnxruntime` is no longer imported, which saves its memory in every engine process.
  - `tests/test_embeddings.py` pins the numpy output to vectors the ONNX path produced (largest
    difference 6e-8 when captured) and pins the matrix hash.
  - The persistent retrieval cache key changed from `local-onnx` to `local-projection`.
- **Version.** `app.__version__ = "1.0.0"`, reported in READY.
- **Dependencies** (`requirements.txt`).
  - Removed: `keyring`, `requests`, `ddgs` and `onnxruntime`; `pyinstaller` and `onnx` were
    removed from `requirements-dev.txt`.
  - Added: `httpx`.
  - Kept, but outside the CONTRACTS §1 dependency list: `numpy`, `scikit-learn`, `pillow`,
    `tzdata` (open question 1).
- **Measuring.** `tools/measure_worker.py` starts the worker the way the supervisor does, with
  fake secrets and a services URL nothing listens on. It reports cold start to READY, `/livez`
  latency, resident memory and stop time. On one Windows development machine, over 5 runs:
  - cold start to READY: median 1.93 s;
  - resident memory after `/livez`: median 143.0 MB, down from 159.9 MB before `onnxruntime`
    was dropped;
  - `/livez`: about 1.1 ms;
  - graceful stop: about 0.23 s, exit code 0.

  These numbers are indicative only, because production engines run on Linux.

## 5. Removed modules

The removed files remain in the pre-conversion source snapshot; they are not in this repository.

| File | What it did | Why it is gone |
|---|---|---|
| `app/bootstrap.py` | handed the local bearer token to the extension over HTTP | bootstrap removed (§3.3); the gateway holds tokens |
| `app/bridge.py` | picked a local model for the machine | no local AI |
| `app/browser_engine.py`, `app/browser_generation.py` | in-browser model engine | browser engine removed (§3.3) |
| `app/events.py` | event hub pushed to the extension | no browser connects to the engine |
| `app/hardware.py` | RAM and GPU budgets for local models | no local AI |
| `app/judge.py` | local judge model lane | no local AI |
| `app/local_model.py` | local writer model | no local AI |
| `app/packed.py` | obfuscated build-time data files | manual is neutral; no installer build |
| `app/permissions.py` | desktop data-folder permission hardening | umask 007 and supervisor-owned folders (§2.1) |
| `app/preflight.py` | end-to-end local model preflight | no local AI |
| `app/seed.py` | downloaded local models at install | no installer |
| `app/throughput.py` | local tokens-per-second measurement | no local AI |
| `app/updater.py` | self-updater | no updater |
| `app/x_source.py` | read an X data export from a folder outside the product | not a hosted data source |
| `run_companion.py` | desktop launcher: port negotiation, native host config | replaced by `app/worker.py` |

## 6. Removed and replaced test files

| File | Reason |
|---|---|
| `test_bridge.py`, `test_hardware.py`, `test_throughput.py`, `test_machine_matrix.py`, `test_model_swap_memory.py`, `test_writer_exists.py`, `test_local_writer_choice.py`, `test_judge_env.py` | local model selection, sizing, installation and serving |
| `test_browser_engine.py`, `test_browser_generation.py` | browser engine removed |
| `test_updater_security.py`, `test_update_recovery.py`, `test_update_skew_contract.py` | updater removed |
| `test_native_host_contract.py`, `test_own_material_wiring.py` | native host chain (extension to native host to companion paths) removed |
| `test_cloud_key_binding.py`, `test_model_provider_routes.py` | user-supplied cloud keys in the OS credential store and provider settings routes removed (§3.3) |
| `test_cloud_provider.py`, old `test_providers.py` | replaced by `test_services_client.py` and a new `test_providers.py` |
| `test_source_registration.py` | feed registration moved to the world service |
| `test_account_topic_fit.py`, `test_approach_drift.py`, `test_lane_pool.py` | calibrated on the original author's archive or captured candidate pool |
| `test_ox_alpha_replay.py` | replay of a captured private session, which is not shipped |
| `test_evaluation_cases.py` | read `evaluation/cases.v1.json`, which is not in this repository |

New test files: `test_worker_contract.py`, `test_services_client.py`, `test_request_cancel.py`,
`test_candidate_variety.py`, plus the renamed `test_llm_enhancement.py`.

## 7. Removed or rewritten individual tests

**Local AI, installer, native host, updater**
- `test_problem_registry.py`: `SPEC1`, `SPEC2`, `SPEC3` (machine budgets, local model and local
  vision fit), `PORT4` (desktop launcher port negotiation), `PORT5` (native host port file).
- `test_paths.py::test_the_native_host_agrees_with_the_companion`.
- `test_measured_not_assumed.py::test_the_preflight_names_the_writer_not_the_bridges_pick`.
- `test_goals_and_reminders.py::test_a_model_too_big_for_the_gpu_is_named_as_the_bottleneck` and
  `::test_a_model_that_fits_is_not_blamed_on_size`.
- `test_deadline.py::test_the_browser_park_cannot_outlast_the_request`. Cancel, fatal-refusal
  and gateway-limit tests were added.
- `test_security_config.py::test_an_existing_install_is_migrated_not_logged_out`, replaced by
  hosted worker-token tests.
- `test_providers.py`: 20 local-runtime routing tests replaced by 9 services-router tests.

**Routes removed by CONTRACTS §3.3** (functions are now tested directly)
- `test_api_bridge.py`: `::test_bridge_context_and_focus_are_observational_only` and
  `::test_bridge_rejects_every_generic_insert_path` were deleted. The lifecycle and snapshot
  tests were rewritten without the removed routes.
- `test_api_intelligence_endpoints.py`, `test_learning_status.py`, `test_sources.py`,
  `test_deleted_and_import_learning.py`: rewritten around the underlying functions.
- `test_reply_dedup.py::test_legacy_draft_generation_route_cannot_bypass_the_duplicate_guard`.
- `test_request_limits.py::test_the_import_path_keeps_its_larger_ceiling`.
- `test_reply_first.py::test_threads_are_not_a_draft_or_insert_contract`, now
  `::test_threads_are_not_a_draft_or_composer_contract`.
- `test_route_auth_matrix.py`: the CORS preflight, OPTIONS and schema tests became "only the
  contract routes exist", "no CORS and no preflight oracle", "removed routes are gone even with
  the token" and "a websocket handshake is refused".
- `test_streaming.py::test_stream_errors_become_final_events_not_500s`, now terminal `error`
  events (§3.4).

**Web sources moved to the world service**
- `test_research.py`: DuckDuckGo link decoding, direct fetch, redirect and SSRF tests, and
  search-based summaries were replaced by world-service tests.
- `test_research_off.py`: rewritten for `/v1/news/headlines`.
- `test_evidence_cache.py::test_a_removed_feed_stops_publishing_immediately` and
  `::test_the_stale_fallback_also_drops_a_removed_feed`.
- `test_topic_supply.py::test_the_pool_has_lab_sources_not_only_show_hn`,
  `::test_atom_feeds_actually_deliver_items` and `::test_the_outside_world_actually_answers`
  (live). `::test_no_single_source_can_flood_the_pool` now reads a fake pool.
- `test_radar_pipeline.py`: the seven X data source tests (`test_only_tier_1_and_2_modules_are_imported`,
  `test_no_tier_3_or_4_module_is_referenced_in_code`, `test_availability_reports_the_tier_and_why`,
  `test_a_missing_scraper_degrades_with_a_reason`, `test_a_rate_limit_backs_off_rather_than_escalating`,
  `test_x_reader_revalidates_addresses_and_never_auto_follows_redirects`,
  `test_handles_beyond_the_cycle_cap_are_reported_not_dropped`).
- `test_registry_sources.py`:
  - `::test_openrouter_arm_finds_ox_alpha`, `::test_hn_host_arm_finds_ox_alpha_with_no_query_string`
    and `::test_openrouter_carried_it_before_hacker_news` read a real capture. Synthetic-capture
    shaping tests replace them.
  - `::test_a_dead_endpoint_returns_empty_rather_than_raising` became
    `::test_the_engine_never_fetches_these_sources`.
- `test_root_causes_two.py::test_hacker_news_reports_its_own_health`, now
  `::test_source_health_reports_merge_rather_than_replace`.
- `test_no_hallucination.py::test_web_lookup_cites_sources`, now
  `::test_web_lookup_never_searches_and_never_invents`.
- `test_visual_analyzer.py::test_openverse_page_size_never_exceeds_the_anonymous_cap`.
- `test_web_images.py::test_image_download_revalidates_redirects_and_never_auto_follows`, now
  `::test_the_hosted_engine_never_downloads_or_searches_images`.

**Tooling not in this repository**
- `test_direction_is_one_path.py::test_the_arena_builds_its_direction_through_the_product_code`.
- `test_topic_audition.py::test_the_material_floor_clears_every_arena_case`. Both read an
  arena tool that was never part of this repository.

**Calibrated on a private post archive** (personal data, never shipped)
- Twenty-one tests across eleven files measured the quality gates against the original author's own
  posts (that a scrubber never rewrites them, that a gate never fires on them). The archive is not
  part of the product, so those tests were removed. The gates themselves are exercised by the rest
  of the suite, and the 19 calibration tests that remain skip unless an archive is supplied
  (open question 9).

**Identity and temperature**
- `test_normal_chat.py::test_the_chat_prompt_no_longer_carries_the_old_name`, now
  `::test_the_chat_prompt_introduces_the_product_by_name`.
- `test_pack_ownership.py::test_the_shipped_pack_now_says_whose_it_is`, now
  `::test_the_shipped_pack_carries_no_identity`.
- `test_reply_quality.py::test_the_default_writer_is_the_confirmed_one`: there is no local
  default writer; the router returns the `starling-quality` alias.
- `test_embeddings.py::test_bundled_onnx_embedder_is_local_and_deterministic`, now
  `::test_bundled_embedder_is_local_and_deterministic`. Added: tests pinning the numpy vectors to
  the ONNX path and pinning the shipped matrix hash.
- `test_strategy_v2.py::test_user_override_wins_and_hash_mismatch_is_reported`: there is no
  per-customer override. It became tests that the shipped manual is craft, not a person, and that
  a changed manual is reported.
- `test_post_attempts.py::test_post_retries_warm_but_never_past_the_cap` and
  `test_quote_attempts.py::test_quote_retries_vary_both_angle_and_temperature`, now same sampling
  with a variation note.

## 8. Optional extras (not in phase 1)

- `/import` from a file path (CONTRACTS §3.3).
- Licensed image search. It needs a world-service route; the ranking and rights logic are still
  in `app/images.py`.
- Publisher preview images. They need a preview image URL in the `/v1/fetch` response.
- General web search and free-text lookups. Free text to the world service is not allowed by
  CONTRACTS §4.2.

## 9. Open questions

1. **Dependencies.** CONTRACTS §1 limits dependencies and says anything else needs a note. The
   engine also needs `numpy` (the embedder and scoring), `scikit-learn` (TF-IDF and similarity),
   `pillow` (image inspection) and `tzdata` (time zones on slim images).
2. **World-service response shapes.** CONTRACTS §4.2 does not define the response bodies of
   `GET /v1/news/headlines` or `POST /v1/summary`. The engine's expectations are in
   `news-pool-shape.md`. When checked, the world-service routes were not yet implemented in
   `backend/server`.
3. **`/v1/fetch`.** The response has no preview image field (section 8).
4. **Request id character sets.** The engine's cancel route and request context accept
   `[A-Za-z0-9._:-]{1,128}`, but the services proxy accepts `X-Starling-Request-Id` only as
   `[A-Za-z0-9_-]{8,128}`. The engine forwards the id verbatim. UUID reservation ids satisfy
   both; the contract should state one set.
5. **Former model file (resolved).** The ONNX file that held the projection is no longer in the
   repository: the matrix ships as `models/starling-embedding.npy`, and
   `tools/build_embedding_projection.py` proves it is exactly the seeded projection.
6. **Root config file (resolved).** The engine reads no product config file, and the old repo-root
   config file has been removed from the repository.
7. **Local runtime names.** Test fixtures that used the former runtime's brand name now name
   vLLM. `app/subject.py` does not recognise vLLM as a name: it starts with a lower-case letter
   and is not in `_SUBJECT_ALIASES` or `_VIBE_CORE`. Add it there if such topics should count as
   named.
8. **Release allowlist (resolved).** The credential-naming test assembles its fake keys at run time,
   and its entry in `scripts/release_allowlist.txt` has been removed.
9. **Always-skipped tests.** 19 tests across 7 files skip in every run because they calibrate
    against the original author's private post archive, which is personal data and is not shipped
    (13 of them through the `_archive()` helper in `test_gate_calibration.py`). They run again for
    anyone who supplies an archive of their own posts.
