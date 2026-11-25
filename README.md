# Local LLM API

Localhost-only OpenAI-compatible API for downloading, hosting, and caching GGUF models with CPU inference.

All development, testing, deployment, and operation must happen inside Docker containers. Do not run host Python or host conda commands for this project.

## Files

- `local_llm.yaml` — model registry, llama.cpp defaults, resource limits, and cache settings.
- `environment.yaml` — Miniconda environment specification.
- `Dockerfile` — Miniconda-based image.
- `docker-compose.yml` — test, dev shell, and API services.
- `opencode.jsonc` — OpenCode providers for the local models.
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

## Commands

Run tests in the container:

```powershell
docker compose run --rm tests
```

The full suite contains 88 cache, config, registry, downloader, resource-preflight, runtime, error-mapping, chat-route, admin-route, and composition tests and has been verified with this command.

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

## OpenCode configuration

Each local model is exposed through its own custom provider because each alias has its own endpoint.

OpenCode V2 uses `limit` singular. Runtime llama.cpp options are sent through `body.options`; there is no supported top-level `options` field.

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

- `docker compose run --rm tests` passes all 88 cache, config, registry, downloader, resource-preflight, runtime, error-mapping, chat-route, admin-route, and composition tests inside the Miniconda container.
- `docker compose --profile serve config` was verified: the API command is `uvicorn local_llm.api:app --host 0.0.0.0 --port 8000`, and the host publish is loopback-only (`127.0.0.1:8000`), with a read-only config mount and local `models/` and `.cache/responses/` mounts. `docker compose --profile serve up -d api` was smoke-tested with placeholder GGUF files; `/health` returned `{"status":"ok"}` from inside the container. `git check-ignore -v models/ .cache/responses/` confirms both paths are ignored.
- Real-model smoke test: **explicitly skipped**. No public model weights were downloaded during MVP verification. Contract tests use a fake Hugging Face CLI and a fake Llama runtime inside Docker, so real HF downloads and real GGUF CPU inference remain unverified. Cached `stream: true` responses are replayed as SSE frames reconstructed from stored complete responses by design, not true incremental streaming generation.
