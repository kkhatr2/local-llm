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
- Use `docker compose --profile serve config --no-interpolate` when sharing Compose output; plain `docker compose config` resolves and prints `HF_TOKEN` when configured.
- GGUF only. Do not add conversion pipelines in MVP.
- Do not enforce a 7B parameter cap. The user verifies model size before load.
- Resource preflight must fail closed using configurable RAM/disk thresholds.
- A model is loaded only when its own chat endpoint is called. A successful response keeps it resident for `server.idle_unload_seconds` (default `180`, valid `> 0` and `<= 86400`) after the last prompt; a prompt for the loaded alias or a cache hit resets the timer, a different alias unloads it immediately, explicit admin unload cancels the timer, and errors unload immediately. Load preflight is skipped only while the same alias is still resident; one model instance and one process lock remain.
- Cache completed responses, not loaded model instances.
- On a `stream: true` cache miss, use native llama.cpp `stream=True` chunks and emit them as SSE while holding the lifecycle lock through iteration; cache only the successfully assembled completion. Cache hits keep replaying stored responses. Pre-response errors retain HTTP envelopes; later errors use sanitized SSE error events without `[DONE]`. Close native chunks before unloading on error/early close, release the lock in `finally`, and never cache partial content.
- Merge llama.cpp options in this order: `llama_defaults`, model `options`, request `options`.
- Default llama.cpp options are `n_ctx: 16384`, `n_batch: 512`, `flash_attn: true`.
- Default sampling options are `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.
- The process-wide model lock waits `server.lock_timeout_seconds` (default `600` seconds); contention still returns `503`.
- GGUF download I/O must not hold the process-wide model lock. Use the injected per-alias download lock to prevent duplicate same-alias downloads; re-acquire the process lock only for existence re-checks and registry mutation. Startup downloads run concurrently up to `download.max_parallel_downloads` (default `4`, valid `> 0` and `<= 16`) after all fail-closed preflight checks pass.
- OpenCode local providers cap output at `96` tokens through both `limit.output` and `body.options.max_tokens`; idle retention reduces repeated cold loads but does not change the cap.
- OpenCode V2 uses `limit` singular. Runtime options sent to this API belong under `body.options`; do not introduce `limits` or top-level `options` keys.
- The default Hugging Face CLI command is `hf`. Do not switch back to `huggingface-cli`: the installed `huggingface_hub` 2.x entry point imports the removed `huggingface_hub.cli.deprecated_cli` module.
- `HF_TOKEN` optionally comes from the untracked repo-root `private.txt` through Compose `env_file` for `api`, `tests`, and `dev`; when absent, public downloads proceed unauthenticated. Never commit it, copy it into the image, pass it as a CLI argument, or log its value.
- `environment.yaml` is the human-edited dependency source; `environment-linux-64.lock` is the generated build artifact. The Dockerfile must digest-pin the Miniconda base image, install from that lock with `conda create --file`, and never run `conda env create -f environment.yaml` or an unpinned `pip install --upgrade pip`. Multi-architecture locks are deferred.
- Keep HTTP handlers thin. Configuration, download, registry, preflight, runtime, cache, and error mapping remain separate modules.
- Inject filesystem paths, clocks, HTTP clients, and model runtimes at composition boundaries so tests can use fakes.

## Current state

- Implemented: `local_llm/cache.py`, `local_llm/config.py`, `local_llm/registry.py`, `local_llm/downloader.py`, `local_llm/preflight.py`, `local_llm/runtime.py`, `local_llm/error_mapper.py`, `local_llm/locking.py`, `local_llm/streaming.py`, `local_llm/chat_routes.py`, `local_llm/admin_routes.py`, `local_llm/composition.py`, `local_llm/api.py` (production ASGI entry point, `app = build_app()`), cache tests, config tests, registry tests, downloader tests, resource-preflight tests, runtime tests, streaming-assembly tests, error-mapping tests, chat-route tests, admin-route tests, composition tests, alias-download-lock tests, dependency-lock tests, OpenCode-config tests, container-environment tests, Docker artifacts, `environment-linux-64.lock`, `local_llm.yaml`, OpenCode local provider configuration, `AGENTS.md`, and `docs/architecture.md`.
- Decisions closed: `ASSUMPTION-1`–`ASSUMPTION-16` are recorded in `docs/architecture.md`; closing a decision is not an implementation claim.
- No application components are pending for MVP. Only deferred, non-MVP items remain in `docs/requirements.md` (for example, multi-architecture dependency locks beyond the committed linux-64 lock).
- Top-seven hardening plan: baseline `3d7e43a Baseline local API and OpenCode configuration` was pushed to `origin/main` before planning; `AC-49`–`AC-56` are documentation/verification gates. `DECISION-21` then changed item 4 to 180-second idle retention; `AC-57`–`AC-61` cover that behavior and are verified. `DECISION-22` closes `SEC-G-01` with per-alias download locks and bounded parallel startup downloads; `AC-62`–`AC-66` cover that change and are verified. `DECISION-23` adds native incremental streaming on cache misses with completed-response caching and cleanup; `AC-67`–`AC-72` are verified. `DECISION-24` makes `HF_TOKEN` optional while preserving token hygiene; `AC-73`–`AC-76` are verified. `DECISION-25` commits a linux-64 dependency lock and digest-pins the amd64 base; `AC-77`–`AC-80` are verified. `ASSUMPTION-17` remains open.
- Security report: `docs/security-report.md` is a living draft of implemented controls, prioritized gaps, severity, effort, team-memory findings, and maintenance triggers. It must not close `ASSUMPTION-17` or implement auth/rate limiting, non-root container, or cache encryption without a product decision.
- Verified: `docker compose run --rm tests` passes all 142 cache, config, registry, downloader, resource-preflight, runtime, streaming-assembly, error-mapping, chat-route, admin-route, composition, alias-download-lock, dependency-lock, OpenCode-config, and container-environment tests inside the Miniconda container, including idle retention, download-lock scope, bounded parallel startup downloads, native incremental streaming, complete-response assembly, sanitized stream errors, early-close/never-started-body lock release, reproducible-build checks, and a captured model output for `what is your name, respond using minimal tokens?`. `docker compose --profile serve config --no-interpolate` confirms the `local_llm.api:app` command, `env_file` wiring, and loopback-only host publish without printing `HF_TOKEN`. An API container startup smoke with placeholder GGUF files returned `{"status":"ok"}` from `/health` and logged per-model download status. A token-authenticated real-model smoke then downloaded `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` with `hf` without the unauthenticated warning; `/health`, `/v1/models`, and chat returned `200` from host loopback with content `I am Qwen.` for the prompt, repeat replayed the cached response, admin unload returned `200`, admin delete returned `204`, and weights/cache entries were removed afterward. A three-prompt OpenCode end-to-end smoke (`AC-61`) in a fresh standalone session (`ses_local-llm-idle-e2e-20261127-1600`) returned `Hello! How can I assist you today?`, `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.`, and `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.` for `hi`, `what is your name?`, and `repeat your answer in one short sentence.` A real-model streaming smoke (`AC-72`) against the Docker API over loopback emitted 27 SSE frames with 24 content deltas and `[DONE]` for a divide-function prompt; the equivalent non-streaming request returned the assembled cached completion with the same ID/created timestamp, and a repeated streaming request replayed one cached content delta plus `[DONE]`. A forced `curl` disconnect during a separate fresh stream was followed by admin unload returning `{"unloaded":false}` with exit `0`, confirming disconnect cleanup had already closed chunks, unloaded the model, and released the lifecycle lock. With `private.txt` absent, Compose config and the full suite also passed, and a no-token real-model smoke downloaded the public GGUF with `HF_TOKEN` absent in both dev and API containers, then returned `I am Qwen.` for the name prompt (`AC-73`–`AC-76`).

## Definition of done

A change is done only when:

1. The relevant acceptance criterion in `docs/requirements.md` is observable.
2. Tests pass inside Docker.
3. No model weights or cache files are staged or committed.
4. Docs are updated instead of leaving stale instructions.
5. OpenCode configuration still validates and uses supported V2 fields.
6. For the top-seven hardening plan, `AC-49`–`AC-56` remain verified; for `DECISION-21`, `AC-57`–`AC-60` are verified inside Docker and `AC-61` is verified with a three-prompt same-alias OpenCode end-to-end smoke; for `DECISION-22`, `AC-62`–`AC-66` are verified inside Docker; for `DECISION-23`, `AC-67`–`AC-72` are verified inside Docker with a real-model streaming smoke; for `DECISION-24`, `AC-73`–`AC-76` are verified inside Docker with and without `private.txt`; for `DECISION-25`, `AC-77`–`AC-80` are verified inside Docker from a clean no-cache amd64 build with byte-for-byte lock regeneration; and `ASSUMPTION-17` is not silently closed.
