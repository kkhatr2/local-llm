# Local LLM API

Localhost-only OpenAI-compatible API for downloading, hosting, and caching GGUF models with CPU inference.

All development, testing, deployment, and operation must happen inside Docker containers. Do not run host Python or host conda commands for this project.

## Files

- `local_llm.yaml` — model registry, llama.cpp defaults, resource limits, and cache settings.
- `environment.yaml` — Miniconda environment specification.
- `Dockerfile` — Miniconda-based image.
- `docker-compose.yml` — test, dev shell, and API services.
- `opencode.jsonc` — OpenCode providers for the local models.
- `private.txt` — optional, untracked `HF_TOKEN=<token>` file injected into every Compose service for faster public-model downloads; public downloads work without it.
- `local_llm/cache.py` — implemented response cache.
- `local_llm/config.py` — implemented configuration loader and immutable settings.
- `local_llm/registry.py` — implemented in-memory model registry.
- `local_llm/downloader.py` — implemented Hugging Face CLI downloader.
- `local_llm/preflight.py` — implemented fail-closed resource checks.
- `local_llm/runtime.py` — implemented lazy llama.cpp runtime adapter.
- `local_llm/chat_routes.py` — implemented health, model-list, and chat completion routes.
- `local_llm/admin_routes.py` — implemented runtime add, unload, and remove routes.
- `local_llm/composition.py` — injectable FastAPI app builder and stable error handlers.
- `local_llm/api.py` — production ASGI entry point that exposes `app = build_app()` for `uvicorn local_llm.api:app`.
- `local_llm/error_mapper.py` — implemented sanitized HTTP error envelopes.
- `tests/` — containerized test suite.
- `AGENTS.md` — repository operating rules.
- `docs/requirements.md` — approved requirements and acceptance criteria.
- `docs/architecture.md` — component, sequence, state, and deployment views.
- [docs/api.md](docs/api.md) — operator runbook: Docker workflow and HTTP endpoint reference.
- [docs/security-report.md](docs/security-report.md) — living security posture: implemented controls, gaps, severity, effort, and review triggers.

## Commands

Run tests in the container:

```powershell
docker compose run --rm tests
```

The full suite contains 136 cache, config, registry, downloader, resource-preflight, runtime, streaming-assembly, error-mapping, chat-route, admin-route, composition, OpenCode-config, and container-environment tests and has been verified with this command both with and without `private.txt`. It includes incremental-streaming, stream-error/cancellation, cache-assembly, lock-release, optional-token, and Compose-boundary coverage, plus a test that captures the model output for `what is your name, respond using minimal tokens?`.

`docker-compose.yml` optionally injects `HF_TOKEN` from the untracked `private.txt` into the `api`, `tests`, and `dev` services. The `env_file` entries use `required: false`, so Compose starts without the file and public GGUF downloads proceed unauthenticated. When present, `hf` and `huggingface_hub` read the variable automatically, so no token is passed on the command line or written to application code.

Configuration path: `LOCAL_LLM_CONFIG` selects the YAML file; an explicit path argument takes precedence, then the env var, then repo-root `local_llm.yaml`.

Open a development shell in the container:

```powershell
docker compose run --rm dev
```

Deploy the API:

```powershell
docker compose --profile serve up -d api
```

Check health:

```powershell
curl http://127.0.0.1:8000/health
```

View logs:

```powershell
docker compose --profile serve logs -f api
```

Startup logs show download status for each enabled model: `downloading model: ...` while the CLI runs, `model download complete: ...` on success, and `model already present: ...; skipping download` when the GGUF already exists.

Stop the API:

```powershell
docker compose --profile serve down
```

The container listens on `0.0.0.0:8000` internally so Docker can publish it. Compose publishes only to `127.0.0.1:8000` on the host.

## API routes

- `GET /health` — liveness check that does not load a model.
- `GET /v1/models` — registered aliases in OpenAI-compatible model-list shape.
- `POST /v1/models/{alias}/chat/completions` — JSON or SSE chat completion for one alias.
- `POST /admin/models` — download and register a public GGUF model at runtime.
- `POST /admin/models/{alias}/unload` — unload that alias's in-memory instance; no-op if not loaded.
- `DELETE /admin/models/{alias}` — unregister the alias without rewriting `local_llm.yaml`.

Runtime-added aliases are ephemeral; copy a desired entry into `local_llm.yaml` to make it permanent.

## Model configuration

`local_llm.yaml` contains the defaults:

```yaml
llama_defaults:
  n_ctx: 16384
  n_batch: 512
  flash_attn: true
  n_threads: 8
  n_gpu_layers: 0
  verbose: false

generation_defaults:
  temperature: 0.1
  top_p: 0.2
  top_k: 20
  min_p: 0.08
  repeat_penalty: 1.1
  max_tokens: 512
```

Effective llama.cpp settings are merged in this order:

1. `llama_defaults`
2. the selected model’s `options`
3. per-request `options` from the chat request body

No parameter-count cap is enforced. The user is responsible for verifying model size and resource requirements before loading a model: configurable, fail-closed preflight checks disk before download and memory before load, but a model is never rejected solely for having an unknown or large parameter count.

Downloaded GGUF files are stored in `models/<alias>/` and are ignored by Git.

## Response cache

Completed chat responses are cached under `.cache/responses/`. Cache entries are keyed by the full normalized request, including messages, sampling settings, model revision, weight fingerprint, and runtime `options`. The `stream` field is excluded so streaming and non-streaming requests can share cache entries.

For a streaming cache miss, native llama.cpp chunks are forwarded as SSE as they arrive and assembled into one completed response for caching after success. Cache hits still replay the stored response. The lifecycle lock remains held throughout live generation; errors or early close release it, unload immediately, and never cache partial content. Errors after SSE starts are sanitized error events without `[DONE]`; pre-response errors retain normal HTTP status envelopes.

Cache settings are in `local_llm.yaml`:

```yaml
cache:
  enabled: true
  dir: .cache/responses
  ttl_days: 30
  max_size_mb: 1024
  replay_sse: true
```

Cache files are ignored by Git.

`.cache/responses/` may contain response text and any user content echoed by the model; the prompt participates in the SHA-256 key but is not stored verbatim. Treat the directory as sensitive local data, restrict filesystem access to the container/host owner, and do not put secrets in prompts. The 30-day TTL and 1 GiB cap are retention limits, not encryption.

## Security and top-seven plan

The 2026-11-27 plan for the seven most desirable features preserves the full-request cache with no bypass, loopback-only exposure without auth, fail-closed preflight, Docker-only execution, optional `private.txt` → `HF_TOKEN` injection, and no 7B cap. Item 4 was then changed by user decision: the GGUF is still loaded lazily, but it stays resident for an idle window after the last prompt instead of unloading immediately. The baseline commit `3d7e43a` was pushed to `origin/main` before the plan was written.

Security boundaries for this plan:

- `127.0.0.1:8000` is the only host publish. Every local host process is trusted; do not publish the API on `0.0.0.0` or a LAN address.
- Admin `POST /admin/models` is a synchronous public-model download. Prefer startup-configured aliases, keep the configured timeout, and rely on disk/RAM preflight; async jobs and approved-alias gating are deferred.
- Startup downloads are bounded by `download.max_parallel_downloads` (default `4`; valid `> 0` and `<= 16`) and run only after fail-closed preflight checks. On-demand GGUF restoration runs under a per-alias download lock, never under the process-wide lifecycle lock.
- `DELETE /admin/models/{alias}` unregisters only. Call the unload endpoint first if an in-memory instance must be released.
- Use `docker compose --profile serve config --no-interpolate` when sharing configuration; plain `docker compose config` resolves and prints `HF_TOKEN` when it is configured.
- Auth/rate limiting, non-root container, cache encryption, content-hash fingerprints, and concurrent generation are deferred product decisions under `ASSUMPTION-17`.

See `docs/requirements.md` for `AC-49` through `AC-76`, `docs/architecture.md` for the component/security boundary mapping, and `docs/security-report.md` for the living mitigation report. That report remains a draft while `ASSUMPTION-17` is open.

## OpenCode configuration

Each local model is exposed through its own custom provider because each alias has its own endpoint.

OpenCode V2 uses `limit` singular. Runtime llama.cpp options are sent through `body.options`; there is no supported top-level `options` field.

Each local provider caps output at 96 tokens in both `limit.output` and `body.options.max_tokens`. A cold cache miss can require a GGUF load, so the cap still avoids long CPU generations; idle retention now prevents repeated loads during a short prompt flow.

The process-wide model lifecycle lock waits `server.lock_timeout_seconds` from `local_llm.yaml` (default `600` seconds). Contention still returns `503 resource_unavailable`; the longer wait accommodates cold CPU inference.

A successful response keeps the requested GGUF resident for `server.idle_unload_seconds` (default `180` seconds; valid `> 0` and `<= 86400` seconds / 24 hours). A follow-up prompt for the loaded alias or a cache hit resets that timer; a prompt for a different alias unloads the resident model immediately; explicit admin unload cancels the timer; and runtime or cache-write errors unload immediately. Load preflight is skipped when the same alias is still resident, and only one model instance exists at a time. The timer uses the same process lock as chat and admin lifecycle operations.

Example selector:

```text
/models
local-phi4/chat
```

Base URL:

```text
http://127.0.0.1:8000/v1/models/phi4-mini
```

To make a local model the default, change the root `"model"` value in `opencode.jsonc` to `local-phi4/chat`.

After editing OpenCode configuration, use `/reload` if the change is not picked up automatically.

## Verification evidence

- The final `docker compose run --rm tests` run passed all 136 cache, config, registry, downloader, resource-preflight, runtime, streaming-assembly, error-mapping, chat-route, admin-route, composition, alias-download-lock, OpenCode-config, and container-environment tests inside the Miniconda container (`Ran 136 tests ... OK`) both with and without `private.txt`, including idle-retention, true incremental-streaming, and optional-token cases. Idle retention (`DECISION-21`, `AC-57`–`AC-60`), download hardening (`DECISION-22`, `AC-62`–`AC-66`), streaming (`DECISION-23`, `AC-67`–`AC-72`), and optional `HF_TOKEN` (`DECISION-24`, `AC-73`–`AC-76`) are verified.
- `docker compose --profile serve config --no-interpolate` was verified: the API command is `uvicorn local_llm.api:app --host 0.0.0.0 --port 8000`, the host publish is loopback-only (`127.0.0.1:8000`), `env_file` is present but optional (`required: false`) for `api`, `tests`, and `dev`, and the config mount is read-only. Use `--no-interpolate` when sharing config output because plain `docker compose config` resolves and prints `HF_TOKEN` when configured. `git check-ignore -v private.txt models/ .cache/responses/` confirms all three paths are ignored.
- OpenCode end-to-end smoke (`AC-61`): **passed** with a fresh standalone session (`ses_local-llm-idle-e2e-20261127-1600`) using `local-qwen-coder-05b/chat`. `hi` returned `Hello! How can I assist you today?`; follow-up `what is your name?` returned `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.`; follow-up `repeat your answer in one short sentence.` returned `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.` All three calls were served by the Docker API over loopback.
- Real-model streaming smoke (`AC-72`): **passed against the Docker API over host loopback** with `qwen25-coder-05b`. A cache-miss request for a short Python divide function returned 27 SSE frames with 24 separate content deltas and `[DONE]`; the equivalent non-streaming request returned the assembled completion with the same ID/created timestamp, and a repeated streaming request replayed one cached content delta plus `[DONE]`. A forced `curl` disconnect during a separate fresh stream was followed by admin unload returning `{"unloaded":false}` with exit `0`, confirming disconnect cleanup. OpenCode V2/96-token contract tests remain green.
- Optional-token smoke (`AC-73`–`AC-76`): **passed** with `private.txt` temporarily absent. Compose config and all 136 tests succeeded, the dev and API containers reported no `HF_TOKEN`, the public Qwen GGUF downloaded without a token, `/health` became ready, and the name prompt returned `I am Qwen.` The file was restored and the API recreated afterward.
- Real-model smoke test: **passed inside Docker with token-authenticated downloads**. The API container reported `HF_TOKEN configured: True`, and `hf` downloaded `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` without the unauthenticated-request warning. From the host loopback, `GET /health` and `GET /v1/models` returned `200`; `POST /v1/models/qwen25-coder-05b/chat/completions` with `what is your name, respond using minimal tokens?` returned `200` with content `I am Qwen.` (usage 39/5/44). A repeat replayed the cached response, admin unload returned `200`, and admin delete returned `204`. Downloaded weights and cache entries were removed afterward. Contract tests still use a fake Hugging Face CLI and fake Llama runtime. Streaming cache hits replay SSE frames reconstructed from stored complete responses; streaming cache misses use native incremental llama.cpp generation.
