# Local LLM API requirements

Status: **approved for MVP definition**. CompositionRoot (C1), ConfigLoader (C2), ModelRegistry (C3), HfCliDownloader (C4), ResourcePreflight (C5), LlamaRuntimeAdapter (C6), ChatRoutes (C7), AdminRoutes (C8), ResponseCache (C9), and ErrorMapper (C10) are implemented and tested. All application components are complete, including the production ASGI entry point; no product-level blockers remain. The tech lead has closed the implementation-owned assumptions listed below and recorded the decisions in `docs/architecture.md`.

Source: product-lead interview conducted 2026-11-25, plus follow-up decisions for response caching, llama.cpp defaults, OpenCode options, and Docker-only workflows. All application modules and their tests pass inside the Docker test service, including the production ASGI entry point `local_llm.api:app` (`app = build_app()` in `local_llm/api.py`, built by `local_llm/composition.py` with FastAPI lifespan startup wiring). All future development, testing, deployment, and other execution must occur inside Docker containers only.

Verification evidence: `docker compose run --rm tests` completed with `Ran 89 tests ... OK` (including composition tests); `docker compose --profile serve config` was verified with the provided Docker Compose binary, confirming the `uvicorn local_llm.api:app --host 0.0.0.0 --port 8000` command, loopback-only host publishing (`127.0.0.1:8000`), a read-only config mount, and local model/cache mounts; `git check-ignore -v models/ .cache/responses/` reports both paths as ignored. The real-model smoke test (criterion 28) is **explicitly skipped**: no public model weights were downloaded during MVP verification. Contract tests use a fake Hugging Face CLI and a fake Llama runtime inside Docker, so real HF downloads and real GGUF CPU inference remain unverified. Cached `stream: true` responses are replayed as SSE frames reconstructed from stored complete responses by design, not true incremental streaming generation.

## Original request

> I want an api created which will download models from using huggingface cli and expose them on localhost so they can be configured into opencode for use. I have created an environment.yaml and sample.py which loads models already saved on disk. I want the framework to be configurable so that any model can be chosen for download and be hosted for CPU inference, if the user chooses models larger than 7b parameters, then this is not allowed. The models should be downloaded to the local directory and the directory should be added to .gitignore so the model weights are not committed to the repo.

The 7B restriction was later removed by the client and is therefore **not** part of the final MVP.

## Outcome

A localhost-only API that:

1. Downloads public GGUF models from Hugging Face into `models/` at the repo root.
2. Registers multiple models and exposes them under a `/v1` model registry.
3. Serves each model at its own chat endpoint.
4. Loads a model into memory only when its endpoint is called.
5. Removes the model from memory after the response is generated, including after SSE streams finish.
6. Performs configurable, fail-closed resource preflight before download/load.
7. Provides `/health`, `/v1/models`, and per-model `/v1/models/{alias}/chat/completions`.
8. Can be configured into OpenCode with one base URL per alias.
9. Caches completed responses locally and replays cache hits without loading a model or running inference.
10. Uses default llama.cpp load settings of `n_ctx: 16384`, `n_batch: 512`, and `flash_attn: true`, plus sampling defaults of `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.
11. Is developed, tested, deployed, and operated only through Docker containers, using Miniconda-based reproducible environments.

## Interview record

### Round 1

| Question | Client answer |
| --- | --- |
| How should model selection work in the MVP? | The API should be able to host multiple models; these multiple models should be available at different endpoints. |
| Which model formats should be accepted? | GGUF only. |
| How should the 7B limit be enforced? | Do not enforce this limit; the user should verify this before trying to load the model. Also do some preload analysis to determine whether there are enough resources on the system to load and serve the model. |
| Where should downloaded weights live? | `models/` at repo root. |
| What API surface is in scope for MVP? | Health + models + chat. |

### Round 2

| Question | Client answer |
| --- | --- |
| How should each hosted model be exposed at a different endpoint? | Registry under `/v1`, with the added limitation that the model should be loaded in memory only if its endpoint is called and removed from memory after a response is generated. |
| How should models be registered and downloaded? | Both: config-defined models at startup and runtime admin endpoints. |
| What should happen when preload analysis says the system lacks enough RAM or disk? | Fail closed, with configurable thresholds. |
| Are gated or private Hugging Face models in scope? | Public models only. |

### Round 3: local caching follow-up

| Question | Client answer |
| --- | --- |
| What should be cached locally? | Completed responses. |
| What should count as the same model run? | Full normalized request. |
| Where should response cache files live? | `.cache/responses`, gitignored. |
| How should streaming requests interact with the cache? | Cache and replay SSE. |

### Round 4: llama.cpp defaults and execution environment

| Client instruction | Interpretation |
| --- | --- |
| Add appropriate defaults for llama.cpp loading: `n_ctx: 16384`, `n_batch: 512`, `flash_attn: true`. | These become `llama_defaults` in `local_llm.yaml`, merged into each model’s runtime settings unless overridden. |
| In OpenCode use `limits` and `options`. | OpenCode V2 documents `limit` singular and provider-package options through `settings`, `headers`, and `body`. The implemented pattern uses `limit` plus `body.options`; unsupported key spellings are not used. |
| Do not use local resources; use only Docker containers for development, testing, deployment, and other tasks. | No host Python/conda execution after this decision. Build, test, run, and deploy through Docker Compose. The host only exposes the API on `127.0.0.1`. |
| Use a package manager such as Miniconda to create reproducible environments. | Docker images derive from a pinned Miniconda base image and create the `local-llm` environment from `environment.yaml`. |

### Round 5: sampling defaults

| Client instruction | Interpretation |
| --- | --- |
| Add `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1` in OpenCode. | These values are added to `generation_defaults` in `local_llm.yaml` and to each local model’s `body.options` in `opencode.jsonc`. |

### Reviewer input folded in

- Tech lead: feasible on Windows CPU with the declared dependencies; use an in-process `llama-cpp-python` adapter, loopback-only host exposure, GGUF-only selection, and explicit concurrency control.
- Delivery manager: dependency order starts with `.gitignore`, config, downloader, health, and verification. Download size/time, HF availability, Windows paths, ignore coverage, and CPU/RAM limits are the main risks.
- Where reviewer recommendations conflicted with client answers, client answers take precedence. The service is not download-only; in-memory CPU inference is in scope.
- Caching completed responses preserves the unload-after-response decision. Keeping loaded models resident was rejected because it conflicts with that decision.
- Container networking modifies the binding requirement: the app binds `0.0.0.0` inside the container so Docker can publish it, while Compose publishes only to `127.0.0.1` on the host.

## Final product decisions

| ID | Decision | Owner | Status |
| --- | --- | --- | --- |
| `DECISION-1` | Use Hugging Face CLI to download models. | Client | Closed |
| `DECISION-2` | GGUF only. | Client | Closed |
| `DECISION-3` | Do not enforce a 7B parameter limit; user verifies before loading. | Client | Closed |
| `DECISION-4` | Run preload/resource analysis; fail closed if RAM/disk are insufficient; thresholds configurable. | Client | Closed |
| `DECISION-5` | Downloaded weights live in `models/` at repo root and are ignored by Git. | Client | Closed |
| `DECISION-6` | API surface includes `/health`, `/v1/models`, and chat. | Client | Closed |
| `DECISION-7` | Multiple models are available at different endpoints. | Client | Closed |
| `DECISION-8` | Endpoint layout is registry under `/v1`: global `/v1/models`, per-model `/v1/models/{alias}/chat/completions`. | Client | Closed |
| `DECISION-9` | Models load only when their endpoint is called and unload after response generation. | Client | Closed |
| `DECISION-10` | Support both startup config-defined models and runtime admin endpoints. | Client | Closed |
| `DECISION-11` | Public Hugging Face models only; no gated/private repos in MVP. | Client | Closed |
| `DECISION-12` | Cache completed responses on disk, not loaded model instances. | Client | Closed |
| `DECISION-13` | Cache key is the full normalized model run request. | Client | Closed |
| `DECISION-14` | Cache files live in `.cache/responses` and are ignored by Git. | Client | Closed |
| `DECISION-15` | Streaming responses are cached and replayed as SSE. | Client | Closed |
| `DECISION-16` | Default llama.cpp load settings are `n_ctx: 16384`, `n_batch: 512`, and `flash_attn: true`. | Client | Closed |
| `DECISION-17` | OpenCode uses `limit` singular and `body.options`; unsupported `limits`/top-level `options` keys are not used. | Client + Tech Lead | Closed with correction |
| `DECISION-18` | All development, testing, deployment, and other execution happens in Docker containers only. | Client | Closed |
| `DECISION-19` | Environments are created with Miniconda from `environment.yaml` inside Docker. | Client | Closed |
| `DECISION-20` | Default sampling settings are `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`, sent through OpenCode `body.options`. | Client | Closed |

## MVP must-haves

- Config-driven model registry with multiple aliases.
- Startup download for missing configured model files.
- Runtime admin capability to add/register, unload, and remove models without restart.
- Hugging Face CLI download into `models/<alias>/`.
- GGUF filename validation.
- No parameter-count rejection.
- Configurable resource preflight:
  - disk check before download;
  - RAM/memory check before load.
- `/health` must work without loading any model.
- `/v1/models` must list registered aliases without exposing filesystem paths.
- Per-model OpenAI-compatible chat endpoint.
- Lazy load on first endpoint call.
- Unload after non-streaming response or after SSE `[DONE]`.
- On-disk cache for completed chat responses, keyed by the full normalized request.
- Cache hit must return the cached response without loading a GGUF or running inference.
- Cache miss must store the completed response atomically after generation.
- SSE replay for cached responses when `stream: true`.
- Configurable cache TTL and maximum cache size.
- `llama_defaults` applied to every model load, with per-model overrides.
- Sampling defaults applied to every generation, with per-request overrides.
- OpenCode model entries use `limit` and `body.options`.
- Docker-only development, testing, deployment, and operations.
- Miniconda-based environment creation from `environment.yaml`.
- Stable error envelope.
- Host-only loopback exposure through Docker port publishing.
- README configuration guide for OpenCode and Docker.

## Deferred

- Gated/private Hugging Face model support and `HF_TOKEN`.
- Asynchronous download job queue; MVP admin downloads may run synchronously with a timeout.
- Cancellation endpoint.
- Embeddings endpoint.
- Tool/function calling.
- GPU inference.
- LAN binding on the host.
- Authentication/authorization beyond host loopback isolation.
- Model-management UI.
- Non-GGUF conversion pipeline.
- Concurrent multi-model residency and concurrent generation.
- Per-request cache bypass controls.
- Cache administration endpoints.
- Automatic OpenCode config rewriting.
- Strict locked dependency file generated from inside Docker.

## Rejected

- 7B parameter enforcement.
- Committing model weights.
- Non-GGUF formats.
- Gated/private model support beyond public repos.
- Loading all registered models into memory at startup.
- Keeping loaded models resident between requests as a cache.
- Host-local Python or conda execution for development, testing, deployment, or other tasks after `DECISION-18`.
- OpenCode key spellings `limits` and top-level `options`, because OpenCode V2 documents `limit` and provider options through `settings`/`headers`/`body`.

## Configuration recommendation

Use four configuration layers:

1. `local_llm.yaml` — source of truth for the local API server, model registry, llama.cpp defaults, resource thresholds, runtime options, and cache settings.
2. `.env` — machine-specific overrides for container paths, host port, and environment selection. Because MVP supports public models only, no Hugging Face token should be needed or stored.
3. `Dockerfile` and `docker-compose.yml` — reproducible build, test, run, and deployment path using a pinned Miniconda base image.
4. `opencode.jsonc` — one custom OpenAI-compatible provider per model alias, each pointing at that alias’s endpoint and declaring limits/options with supported V2 fields.

This keeps model identity, download details, resource policy, runtime options, cache policy, and OpenCode selection explicit and reviewable.

### Recommended `local_llm.yaml`

```yaml
server:
  # Inside the container the app must listen on 0.0.0.0 so Docker can publish it.
  # Compose publishes only to 127.0.0.1 on the host.
  host: 0.0.0.0
  port: 8000

paths:
  model_dir: models
  cache_dir: .cache/responses

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

max_n_ctx: 32768

cache:
  enabled: true
  dir: .cache/responses
  ttl_days: 30
  max_size_mb: 1024
  replay_sse: true

download:
  command: huggingface-cli
  timeout_seconds: 1800
  public_only: true

resources:
  min_free_disk_gb: 10
  reserve_disk_gb: 2
  max_estimated_model_memory_gb: 6
  reserve_system_memory_gb: 4
  fail_closed: true

models:
  - alias: phi4-mini
    enabled: true
    repo_id: bartowski/microsoft_Phi-4-mini-instruct-GGUF
    filename: microsoft_Phi-4-mini-instruct-Q4_K_M.gguf
    revision: main
    options:
      n_ctx: 16384
      n_batch: 512
      flash_attn: true
      n_threads: 8

  - alias: qwen25-coder-3b
    enabled: true
    repo_id: Qwen/Qwen2.5-Coder-3B-Instruct-GGUF
    filename: qwen2.5-coder-3b-instruct-q4_k_m.gguf
    revision: main
    options:
      n_ctx: 16384
      n_batch: 512
      flash_attn: true
      n_threads: 8
```

Effective llama.cpp settings are produced by merging, in order:

1. `llama_defaults`;
2. the selected model’s `options`;
3. per-request `options` from the chat body.

### Validation rules

- `alias`: required, lowercase `a-z`, digits, `_` or `-`; no path separators; unique.
- `repo_id`: required public Hugging Face repository ID in `owner/name` form.
- `filename`: required and must end in `.gguf`.
- `revision`: optional; default `main`.
- `enabled`: optional; default `true`.
- `llama_defaults.n_ctx`: must be positive and no greater than `max_n_ctx`.
- `llama_defaults.n_batch`: must be positive.
- `llama_defaults.flash_attn`: must be boolean.
- Model `options` may override only supported llama.cpp load or sampling fields.
- `runtime.n_threads`: must be positive and no greater than the container’s logical CPU count.
- Resource thresholds must be positive numbers.
- `cache.enabled`, `cache.ttl_days`, `cache.max_size_mb`, and `cache.replay_sse` must be valid typed values.
- `cache.dir` must stay inside the project workspace and may not point into `models/`.
- Duplicate aliases, invalid aliases, missing fields, non-GGUF filenames, or unsupported llama.cpp options must fail startup with a message naming the offending field.

### Download behavior

For each configured model, the downloader should invoke the Hugging Face CLI approximately as:

```sh
huggingface-cli download <repo_id> <filename> --revision <revision> --local-dir models/<alias>
```

Before download:

- reject non-public repos;
- verify enough free disk space using the resource thresholds;
- create `models/<alias>/` only after validation passes.

If the expected GGUF file already exists, skip the CLI call.

All downloads must run inside the API container, with `models/` mounted as a persistent volume.

### Cache behavior

Cache entries represent completed chat responses, not loaded models. A cache hit must avoid model preflight, model loading, and inference.

The cache key should be a SHA-256 hash of canonical JSON containing:

- `alias`;
- model `revision`;
- a resolved weight fingerprint, such as file size plus modification time;
- normalized `messages`, preserving role and content;
- sampling fields such as `temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`, `frequency_penalty`, `presence_penalty`, `max_tokens`, `seed`, `n`, and `stop`;
- request `options`, including `n_ctx`, `n_batch`, and `flash_attn`;
- relevant runtime settings.

The `stream` field must be excluded from the key so a non-streaming completion can satisfy a later streaming request and vice versa. Request IDs, logging metadata, and other transport-only fields must also be excluded.

Each cache file should be JSON containing:

- schema version;
- cache key;
- created timestamp;
- last-accessed timestamp;
- model alias and revision;
- the full OpenAI-compatible response object;
- finish reason and usage metadata when present.

Cache files must be written atomically using a temporary file followed by a rename. Corrupt, incomplete, or schema-incompatible cache files must be treated as cache misses and may be deleted.

Cache hits must:

1. verify the entry is unexpired;
2. update last-accessed metadata;
3. return the cached response immediately;
4. for `stream: true`, replay the complete response as SSE frames followed by `[DONE]`.

Cache misses must:

1. perform normal resource preflight;
2. load only the requested alias;
3. generate the response;
4. write the cache entry;
5. unload the model after generation, including after SSE completion.

Cache size and TTL are enforced from `cache.ttl_days` and `cache.max_size_mb`. Eviction should remove expired entries first, then least-recently-accessed entries until the cache is under the configured limit.

### Runtime admin add request

Runtime-added models should use the same fields as a YAML entry:

```json
{
  "alias": "tinyllama",
  "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
  "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
  "revision": "main",
  "options": {
    "n_ctx": 16384,
    "n_batch": 512,
    "flash_attn": true,
    "n_threads": 8,
    "temperature": 0.1,
    "top_p": 0.2,
    "top_k": 20,
    "min_p": 0.08,
    "repeat_penalty": 1.1
  }
}
```

Recommendation: runtime-added aliases are **ephemeral by default**. To make one permanent, the operator copies its entry into `local_llm.yaml`. This avoids having the server silently rewrite tracked config files.

### OpenCode configuration

Use one custom provider per alias because each model has its own endpoint. Example:

```jsonc title="opencode.jsonc"
{
  "$schema": "https://opencode.ai/config.json",
  "model": "opencode/big-pickle",

  "providers": {
    "local-phi4": {
      "name": "Local Phi-4 Mini",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8000/v1/models/phi4-mini",
        "headerTimeout": 600000,
        "chunkTimeout": 600000,
        "timeout": 1800000,
        "encoding": "none"
      },
      "compatibility": {
        "streamUsage": false
      },
      "models": {
        "chat": {
          "name": "Phi-4 Mini CPU",
          "modelID": "phi4-mini",
          "limit": {
            "context": 16384,
            "output": 512
          },
          "capabilities": {
            "tools": false,
            "input": ["text"],
            "output": ["text"]
          },
          "body": {
            "options": {
              "n_ctx": 16384,
              "n_batch": 512,
              "flash_attn": true,
              "n_threads": 8,
              "temperature": 0.1,
              "top_p": 0.2,
              "top_k": 20,
              "min_p": 0.08,
              "repeat_penalty": 1.1
            }
          }
        }
      }
    }
  }
}
```

OpenCode V2 uses `limit` singular, not `limits`. There is no documented top-level `options` field for providers or models; provider-package options are expressed through `settings`, `headers`, and `body`. Local llama.cpp runtime options are sent to this API through `body.options`.

Then select a local model with `/models`, for example `local-phi4/chat`. To make a local model the default, change the root `"model"` value to that selector.

Config changes apply without a restart; use `/reload` if OpenCode does not pick up a saved change.

### Docker-only workflow

Development, testing, deployment, and operations must run through Docker Compose:

- `docker compose run --rm tests` for unit and contract tests.
- `docker compose run --rm dev` for an interactive container shell.
- `docker compose --profile serve up -d api` to deploy the API.
- `docker compose --profile serve logs -f api` to inspect logs.
- `docker compose --profile serve down` to stop deployment.

The API container listens on `0.0.0.0:8000` internally. Compose publishes it as `127.0.0.1:8000:8000`, so only the host loopback interface can reach it.

Persistent volumes:

- `models/` for downloaded GGUF weights.
- `.cache/responses/` for cached responses.

The image is built from a pinned Miniconda base image and creates the `local-llm` environment from `environment.yaml`. A strict committed lock file is deferred until it can be generated from inside Docker.

## Assumptions needing implementation decisions

These product-level assumptions were closed by the tech lead. The concrete decisions are recorded in `docs/architecture.md` under "Closed implementation assumptions"; closing a decision does not change component status.

| ID | Assumption | Owner | Decision |
| --- | --- | --- | --- |
| `ASSUMPTION-1` | Config lives in a repo-root YAML file with env overrides. | Tech Lead | Closed by ConfigLoader implementation. |
| `ASSUMPTION-2` | Inside Docker the app binds `0.0.0.0:8000`; Compose publishes only to host `127.0.0.1:8000`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-3` | Admin route names will be `POST /admin/models`, `POST /admin/models/{alias}/unload`, and `DELETE /admin/models/{alias}`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-4` | Resource thresholds are configurable with safe defaults. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-5` | Admin endpoints require no auth because the host publish is loopback-only. | Tech Lead + Security Engineer | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-6` | HF CLI invocation uses `huggingface-cli download` with `--local-dir`; revision/filename are passed when configured. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-7` | Generation requests are globally serialized because models are loaded/unloaded per request. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-8` | “Unload after response” means after the final token for streaming responses. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-9` | Runtime-added aliases are ephemeral unless copied into YAML. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-10` | OpenCode integration uses one custom provider per alias. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-11` | Cache keys use canonical JSON plus SHA-256 as described above. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-12` | Default cache limits are 30 days and 1024 MB. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-13` | SSE replay reconstructs chunks from the stored complete response rather than storing raw chunk bytes. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-14` | The installed `llama-cpp-python` version supports `flash_attn`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-15` | OpenCode sends runtime llama options through `body.options`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-16` | Miniconda base image plus `environment.yaml` is sufficient until a committed lock file is generated. | Tech Lead + Delivery Manager | Closed; see `docs/architecture.md`. |

## Acceptance criteria

Each criterion has one observable behavior and an owning module.

1. **[config]** Given a config with at least one valid alias, repo ID, and GGUF filename, startup registers that alias in `/v1/models`.
2. **[config]** Given a missing required field or duplicate alias, startup fails with a message naming the invalid field or alias.
3. **[gitignore]** `.gitignore` contains `models/` and `.cache/`, and `git check-ignore models/ .cache/responses/` reports both paths as ignored.
4. **[downloader]** Given a configured model whose GGUF file is absent, startup invokes the Hugging Face CLI and writes the file under `models/<alias>/`.
5. **[downloader]** Given a configured model whose GGUF file already exists, startup does not invoke the Hugging Face CLI.
6. **[downloader]** Given a gated/private or non-public repo, the downloader rejects it before downloading and returns a clear reason.
7. **[registry]** `GET /v1/models` returns registered aliases in OpenAI-compatible model-list shape and does not expose local filesystem paths.
8. **[health]** `GET /health` returns `200` without loading a model.
9. **[admin]** `POST /admin/models` with a valid public GGUF model downloads and registers a new alias without restarting the server.
10. **[admin]** `POST /admin/models/{alias}/unload` removes that alias’s model instance from memory.
11. **[admin]** `DELETE /admin/models/{alias}` unregisters the alias; its chat endpoint then returns `404`.
12. **[preflight]** Given insufficient disk space, a download request returns a configured `503` failure and writes no model bytes.
13. **[preflight]** Given insufficient RAM for the configured thresholds, a chat request returns a configured `503` failure and does not load the model.
14. **[runtime]** After startup, no `Llama` instance exists for registered aliases.
15. **[runtime]** On a cache miss, calling `POST /v1/models/{alias}/chat/completions` loads only the requested alias’s GGUF.
16. **[runtime]** Alias A’s chat endpoint never loads or unloads Alias B’s model.
17. **[routes]** On a cache miss, `POST /v1/models/{alias}/chat/completions` with a valid OpenAI-style body returns an OpenAI-compatible response.
18. **[routes]** On a cache miss with `stream: true`, the endpoint emits SSE frames and terminates with `[DONE]`.
19. **[runtime]** After a non-streaming response completes, the model instance is no longer in memory.
20. **[runtime]** After a streaming response emits `[DONE]`, the model instance is no longer in memory.
21. **[size-policy]** A model is never rejected solely because its parameter count is unknown or greater than 7B.
22. **[errors]** Invalid request bodies return `422` with the stable error envelope.
23. **[errors]** Hugging Face CLI or runtime failures return sanitized `502`/`504` errors without leaking stack traces or paths.
24. **[security]** The API container listens on `0.0.0.0:8000` internally and Compose publishes it only on host `127.0.0.1:8000`.
25. **[docs]** README shows how to set OpenCode’s custom provider `baseURL` to `http://127.0.0.1:8000/v1/models/{alias}`.
26. **[docs]** README states clearly that the user is responsible for verifying model size before loading.
27. **[tests]** Contract and integration tests using a fake Hugging Face CLI and fake Llama runtime pass inside the test container.
28. **[verification]** A real-model smoke test is either recorded as passing inside Docker or explicitly marked skipped with reason. **Status: explicitly skipped for MVP.** Reason: no public model weights were downloaded during MVP verification; contract tests use a fake Hugging Face CLI and a fake Llama runtime inside Docker, so real HF downloads and real GGUF CPU inference remain unverified.
29. **[cache]** A completed non-streaming response is written as one JSON file under `.cache/responses/`.
30. **[cache]** Repeating the same normalized request returns the stored response without invoking the fake Llama runtime.
31. **[cache]** Repeating the same request with a different sampling parameter does not return the first cached response.
32. **[cache]** A non-streaming cache hit followed by the equivalent `stream: true` request emits SSE frames and `[DONE]` without loading a model.
33. **[cache]** A corrupt cache file is treated as a cache miss and does not fail the chat request.
34. **[cache]** An expired cache entry is treated as a cache miss.
35. **[cache]** When the cache exceeds `max_size_mb`, least-recently-accessed entries are removed until the cache is under the limit.
36. **[docs]** README documents cache location, key inputs, TTL/size settings, and that cached prompts and responses are stored locally.
37. **[llama-config]** Given default config, the runtime passes `n_ctx: 16384`, `n_batch: 512`, and `flash_attn: true` to `Llama(...)`.
38. **[llama-config]** Given a model-level or request-level llama option override, the effective value differs from the default and is the one passed to `Llama(...)`.
39. **[opencode]** OpenCode model entries use `limit` singular and `body.options`; no `limits` or top-level `options` keys are present.
40. **[docker]** `docker compose run --rm tests` runs the full test suite in the Miniconda container.
41. **[docker]** `docker compose --profile serve up -d api` starts the API container with `models/` and `.cache/responses/` mounted as persistent volumes.
42. **[docker]** No development or verification step in the runbook invokes host Python or host conda.
43. **[generation-config]** Given default OpenCode and YAML config, generation uses `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.

## Delivery sequence

1. Add `models/` and `.cache/` to `.gitignore`.
2. Add Docker-only artifacts: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and Miniconda environment creation.
3. Define config schema and validation, including llama defaults, runtime options, and cache settings.
4. Implement Hugging Face CLI downloader.
5. Implement model registry and `/v1/models`.
6. Implement admin add/unload/remove endpoints.
7. Implement configurable resource preflight.
8. Implement lazy runtime adapter and per-model chat routes with merged llama options.
9. Implement SSE streaming plus unload-after-response cleanup.
10. Implement response cache keying, storage, lookup, replay, expiry, and eviction.
11. Add error envelope, host loopback Docker publishing, and cache/runtime tests.
12. Write README and OpenCode configuration example using `limit` and `body.options`.

## Next step

MVP implementation is complete, including the C1 production composition root and ASGI entry point; `ASSUMPTION-1` through `ASSUMPTION-16` are closed and recorded in `docs/architecture.md`. Remaining work is limited to the deferred, non-MVP items listed above. All verification after this point must be executed inside Docker containers.
