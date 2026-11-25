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
- `tests/` — containerized test suite.
- `AGENTS.md` — repository operating rules.
- `docs/requirements.md` — approved requirements and acceptance criteria.
- `docs/architecture.md` — component, sequence, state, and deployment views.

## Commands

Run tests in the container:

```powershell
docker compose run --rm tests
```

The full suite contains 16 cache and config tests and has been verified with this command.

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
