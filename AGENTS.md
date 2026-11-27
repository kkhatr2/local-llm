# AGENTS.md

## Project

Local LLM API: a localhost-only OpenAI-compatible service that downloads public GGUF models with the Hugging Face CLI, hosts multiple models at separate `/v1/models/{alias}` endpoints, runs CPU inference through `llama-cpp-python`, and caches completed responses locally.

## Authoritative documents

- `docs/requirements.md` — approved scope, decisions, assumptions, acceptance criteria, and the current change plan.
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
- Keep cached responses in `.cache/responses/`; never commit them. Treat that directory as sensitive response data and restrict filesystem access.
- Use `docker compose --profile serve config --no-interpolate` when sharing Compose output; plain `docker compose config` resolves and prints `HF_TOKEN`.
- GGUF only. Do not add conversion pipelines in MVP.
- Do not enforce a 7B parameter cap. The user verifies model size before load.
- Resource preflight must fail closed using configurable RAM/disk thresholds.
- A model is loaded only when its own chat endpoint is called. A successful response keeps it resident for `server.idle_unload_seconds` (default `180`, valid `> 0` and `<= 86400`) after the last prompt; a prompt for the loaded alias or a cache hit resets the timer, a different alias unloads it immediately, explicit admin unload cancels the timer, and errors unload immediately. Load preflight is skipped only while the same alias is still resident; one model instance and one process lock remain.
- Cache completed responses, not loaded model instances.
- Merge llama.cpp options in this order: `llama_defaults`, model `options`, request `options`.
- Default llama.cpp options are `n_ctx: 16384`, `n_batch: 512`, `flash_attn: true`.
- Default sampling options are `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.
- The process-wide model lock waits `server.lock_timeout_seconds` (default `600` seconds); contention still returns `503`.
- OpenCode local providers cap output at `96` tokens through both `limit.output` and `body.options.max_tokens`; idle retention reduces repeated cold loads but does not change the cap.
- OpenCode V2 uses `limit` singular. Runtime options sent to this API belong under `body.options`; do not introduce `limits` or top-level `options` keys.
- The default Hugging Face CLI command is `hf`. Do not switch back to `huggingface-cli`: the installed `huggingface_hub` 2.x entry point imports the removed `huggingface_hub.cli.deprecated_cli` module.
- `HF_TOKEN` comes from the untracked repo-root `private.txt` through Compose `env_file` for `api`, `tests`, and `dev`. Never commit it, copy it into the image, pass it as a CLI argument, or log its value.
- Keep HTTP handlers thin. Configuration, download, registry, preflight, runtime, cache, and error mapping remain separate modules.
- Inject filesystem paths, clocks, HTTP clients, and model runtimes at composition boundaries so tests can use fakes.

## Current state

- Implemented: `local_llm/cache.py`, `local_llm/config.py`, `local_llm/registry.py`, `local_llm/downloader.py`, `local_llm/preflight.py`, `local_llm/runtime.py`, `local_llm/error_mapper.py`, `local_llm/locking.py`, `local_llm/chat_routes.py`, `local_llm/admin_routes.py`, `local_llm/composition.py`, `local_llm/api.py` (production ASGI entry point, `app = build_app()`), cache tests, config tests, registry tests, downloader tests, resource-preflight tests, runtime tests, error-mapping tests, chat-route tests, admin-route tests, composition tests, OpenCode-config tests, container-environment tests, Docker artifacts, `local_llm.yaml`, OpenCode local provider configuration, `AGENTS.md`, and `docs/architecture.md`.
- Decisions closed: `ASSUMPTION-1`–`ASSUMPTION-16` are recorded in `docs/architecture.md`; closing a decision is not an implementation claim.
- No application components are pending for MVP. Only deferred, non-MVP items remain in `docs/requirements.md` (for example, the committed dependency lock file from `ASSUMPTION-16`).
- Top-seven hardening plan: baseline `3d7e43a Baseline local API and OpenCode configuration` was pushed to `origin/main` before planning; `AC-49`–`AC-56` are documentation/verification gates. `DECISION-21` then changed item 4 to 180-second idle retention; `AC-57`–`AC-61` cover that behavior and are verified. `ASSUMPTION-17` remains open.
- Verified: `docker compose run --rm tests` passes all 110 cache, config, registry, downloader, resource-preflight, runtime, error-mapping, chat-route, admin-route, composition, OpenCode-config, and container-environment tests inside the Miniconda container, including idle retention and a captured model output for `what is your name, respond using minimal tokens?`. `docker compose --profile serve config --no-interpolate` confirms the `local_llm.api:app` command, `env_file` wiring, and loopback-only host publish without printing `HF_TOKEN`. An API container startup smoke with placeholder GGUF files returned `{"status":"ok"}` from `/health` and logged per-model download status. A token-authenticated real-model smoke then downloaded `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` with `hf` without the unauthenticated warning; `/health`, `/v1/models`, and chat returned `200` from host loopback with content `I am Qwen.` for the prompt, repeat replayed the cached response, admin unload returned `200`, admin delete returned `204`, and weights/cache entries were removed afterward. A three-prompt OpenCode end-to-end smoke (`AC-61`) in a fresh standalone session (`ses_local-llm-idle-e2e-20261127-1600`) returned `Hello! How can I assist you today?`, `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.`, and `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.` for `hi`, `what is your name?`, and `repeat your answer in one short sentence.`

## Definition of done

A change is done only when:

1. The relevant acceptance criterion in `docs/requirements.md` is observable.
2. Tests pass inside Docker.
3. No model weights or cache files are staged or committed.
4. Docs are updated instead of leaving stale instructions.
5. OpenCode configuration still validates and uses supported V2 fields.
6. For the top-seven hardening plan, `AC-49`–`AC-56` remain verified; for `DECISION-21`, `AC-57`–`AC-60` are verified inside Docker and `AC-61` is verified with a three-prompt same-alias OpenCode end-to-end smoke, and `ASSUMPTION-17` is not silently closed.
