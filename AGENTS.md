# AGENTS.md

## Project

Local LLM API: a localhost-only OpenAI-compatible service that downloads public GGUF models with the Hugging Face CLI, hosts multiple models at separate `/v1/models/{alias}` endpoints, runs CPU inference through `llama-cpp-python`, and caches completed responses locally.

## Authoritative documents

- `docs/requirements.md` — approved scope, decisions, assumptions, and acceptance criteria.
- `docs/architecture.md` — UML component, class, sequence, and deployment views.
- `local_llm.yaml` — runtime configuration source of truth.
- `README.md` — operating commands.

If these documents disagree with implementation comments, update the documents first, then the code.

## Execution rules

- Use Docker containers for all development, testing, deployment, and verification.
- Do not run host Python, host conda, or host uvicorn commands.
- Run tests with `docker compose run --rm tests`.
- Open a development shell with `docker compose run --rm dev`.
- Deploy with `docker compose --profile serve up -d api`.
- The container binds `0.0.0.0:8000` internally; Compose publishes only to host `127.0.0.1:8000`.
- Verify Compose changes with `docker compose --profile serve config`.

## Implementation rules

- Preserve the loopback-only host exposure.
- Keep model weights in `models/<alias>/`; never commit them.
- Keep cached responses in `.cache/responses/`; never commit them.
- GGUF only. Do not add conversion pipelines in MVP.
- Do not enforce a 7B parameter cap. The user verifies model size before load.
- Resource preflight must fail closed using configurable RAM/disk thresholds.
- A model is loaded only when its own chat endpoint is called and is unloaded after the response, including after SSE `[DONE]`.
- Cache completed responses, not loaded model instances.
- Merge llama.cpp options in this order: `llama_defaults`, model `options`, request `options`.
- Default llama.cpp options are `n_ctx: 16384`, `n_batch: 512`, `flash_attn: true`.
- Default sampling options are `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.
- OpenCode V2 uses `limit` singular. Runtime options sent to this API belong under `body.options`; do not introduce `limits` or top-level `options` keys.
- Keep HTTP handlers thin. Configuration, download, registry, preflight, runtime, cache, and error mapping remain separate modules.
- Inject filesystem paths, clocks, HTTP clients, and model runtimes at composition boundaries so tests can use fakes.

## Current state

- Implemented: `local_llm/cache.py`, `local_llm/config.py`, `local_llm/registry.py`, `local_llm/downloader.py`, `local_llm/preflight.py`, `local_llm/runtime.py`, `local_llm/error_mapper.py`, `local_llm/chat_routes.py`, `local_llm/composition.py`, cache tests, config tests, registry tests, downloader tests, resource-preflight tests, runtime tests, error-mapping tests, chat-route tests, Docker artifacts, `local_llm.yaml`, OpenCode local provider configuration, `AGENTS.md`, and `docs/architecture.md`.
- Decisions closed: `ASSUMPTION-1`–`ASSUMPTION-16` are recorded in `docs/architecture.md`; closing a decision is not an implementation claim.
- Pending: C8 admin routes and C1 production composition root/FastAPI entry point.
- Verified: `docker compose run --rm tests` passes all 73 cache, config, registry, downloader, resource-preflight, runtime, error-mapping, and chat-route tests inside the Miniconda container.

## Definition of done

A change is done only when:

1. The relevant acceptance criterion in `docs/requirements.md` is observable.
2. Tests pass inside Docker.
3. No model weights or cache files are staged or committed.
4. Docs are updated instead of leaving stale instructions.
5. OpenCode configuration still validates and uses supported V2 fields.
