# Local LLM API requirements

Status: **approved for MVP definition**. CompositionRoot (C1), ConfigLoader (C2), ModelRegistry (C3), HfCliDownloader (C4), ResourcePreflight (C5), LlamaRuntimeAdapter (C6), ChatRoutes (C7), AdminRoutes (C8), ResponseCache (C9), ErrorMapper (C10), AliasDownloadLocks (C15), and ChatStreamAssembler (C16) are implemented and tested. All application components are complete, including the production ASGI entry point; no product-level blockers remain. The tech lead has closed the implementation-owned assumptions listed below and recorded the decisions in `docs/architecture.md`.

Source: product-lead interview conducted 2026-11-25, plus follow-up decisions for response caching, llama.cpp defaults, OpenCode options, and Docker-only workflows. All application modules and their tests pass inside the Docker test service, including the production ASGI entry point `local_llm.api:app` (`app = build_app()` in `local_llm/api.py`, built by `local_llm/composition.py` with FastAPI lifespan startup wiring). All future development, testing, deployment, and other execution must occur inside Docker containers only.

Verification evidence: the baseline `docker compose run --rm tests` completed with `Ran 95 tests ... OK` (including composition, OpenCode-config, and container-environment tests, and a prompt-output capture test), and the final run for `DECISION-21` completed with `Ran 110 tests ... OK`; `docker compose --profile serve config --no-interpolate` was verified with the provided Docker Compose binary, confirming the `uvicorn local_llm.api:app --host 0.0.0.0 --port 8000` command, loopback-only host publishing (`127.0.0.1:8000`), `env_file` wiring for `api`/`tests`/`dev`, a read-only config mount, and local model/cache mounts without printing `HF_TOKEN`; `git check-ignore -v private.txt models/ .cache/responses/` reports all three paths as ignored. The real-model smoke test (criterion 28) **passed inside Docker with token-authenticated downloads** using `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` (alias `qwen25-coder-05b`, file `qwen2.5-coder-0.5b-instruct-q4_k_m.gguf`): the API container reported `HF_TOKEN configured: True`, `hf download` succeeded without the unauthenticated warning, `/health` and `/v1/models` returned `200` from host loopback, and the chat endpoint returned `200` with content `I am Qwen.` for `what is your name, respond using minimal tokens?`. A repeat replayed the cached response; admin unload returned `200` and admin delete returned `204`. The downloaded weights and cache entries were removed after verification. API logs report per-model download status (`downloading model`, `model download complete`, or `model already present; skipping download`); this was verified in the API container. Contract tests still use a fake Hugging Face CLI and a fake Llama runtime. Streaming cache hits are replayed as SSE frames reconstructed from stored complete responses; streaming cache misses use native incremental llama.cpp generation under `DECISION-23`. The post-`DECISION-23` baseline was `Ran 135 tests ... OK`; the current `DECISION-24` run completed with `Ran 136 tests ... OK`, adding native chunk forwarding, completed-response assembly, stream cache interoperability, lock release (including a never-started body), pre-header errors, sanitized mid-stream errors, early-close coverage, and optional-token/Compose-boundary checks on top of download-hardening, idle-retention, and OpenCode-config coverage. Idle retention (`DECISION-21`, `server.idle_unload_seconds` default 180 seconds, validated `> 0` and `<= 86400` / 24 hours) is verified by `AC-57`–`AC-60`. The three-prompt OpenCode end-to-end smoke (`AC-61`) used a fresh standalone session (`ses_local-llm-idle-e2e-20261127-1600`) with the model `local-qwen-coder-05b/chat`: `hi` returned `Hello! How can I assist you today?`; `what is your name?` returned `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.`; `repeat your answer in one short sentence.` returned `I am Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF.` All three calls were served by the Docker API over loopback. `DECISION-22` (`AC-62`–`AC-66`) was verified by the DECISION-22-era 136-test Docker run and remains covered by the current 142-test suite: download I/O runs outside the lifecycle lock, same-alias downloads are serialized, startup downloads overlap within the configured bound, preflight failure starts no download command, config boundaries are accepted/rejected, and failed restores leave registry state unchanged. `DECISION-23` (`AC-67`–`AC-72`) was verified by the DECISION-23-era 136-test Docker run and a real-model loopback smoke, and remains covered by the current 142-test suite: a cache-miss divide-function request emitted 27 SSE frames with 24 content deltas and `[DONE]`; the equivalent non-streaming request returned the assembled completion with the same ID/created timestamp, and a repeated streaming request replayed one cached content delta plus `[DONE]`. A forced `curl` disconnect during a separate fresh stream was followed by admin unload returning `{"unloaded":false}` with exit `0`, confirming disconnect cleanup had already closed chunks, unloaded the model, and released the lifecycle lock. `DECISION-24` (`AC-73`–`AC-76`) was verified by the DECISION-24-era 136-test suite run with and without `private.txt`: Compose config and all tests passed in both states, and a no-token real-model smoke downloaded the public Qwen GGUF with `HF_TOKEN` absent in dev and API containers, reached `/health`, and returned `I am Qwen.` for the name prompt. After `DECISION-25`, Compose config and the current 142-test suite were re-run with and without `private.txt`; both passed. `DECISION-25` (`AC-77`–`AC-80`) is verified by a clean no-cache amd64 image build from the committed lock, a byte-for-byte lock regeneration check inside the dev container, and the current `Ran 142 tests ... OK` suite; the final Docker run is documented below.

## Original request

> I want an api created which will download models from using huggingface cli and expose them on localhost so they can be configured into opencode for use. I have created an environment.yaml and sample.py which loads models already saved on disk. I want the framework to be configurable so that any model can be chosen for download and be hosted for CPU inference, if the user chooses models larger than 7b parameters, then this is not allowed. The models should be downloaded to the local directory and the directory should be added to .gitignore so the model weights are not committed to the repo.

The 7B restriction was later removed by the client and is therefore **not** part of the final MVP.

## Outcome

A localhost-only API that:

1. Downloads public GGUF models from Hugging Face into `models/` at the repo root.
2. Registers multiple models and exposes them under a `/v1` model registry.
3. Serves each model at its own chat endpoint.
4. Loads a model into memory only when its endpoint is called.
5. Keeps a lazily loaded model resident in memory for `server.idle_unload_seconds` (default 180 seconds; valid values `> 0` and `<= 86400` seconds / 24 hours) after the last prompt, then removes it; a prompt for the loaded alias or a cache hit resets the idle timer.
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
| Use a package manager such as Miniconda to create reproducible environments. | Docker images derive from a digest-pinned Miniconda base image and create the `local-llm` environment from the committed linux-64 lock generated from `environment.yaml` inside Docker. |

### Round 5: sampling defaults

| Client instruction | Interpretation |
| --- | --- |
| Add `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1` in OpenCode. | These values are added to `generation_defaults` in `local_llm.yaml` and to each local model’s `body.options` in `opencode.jsonc`. |

### Reviewer input folded in

- Tech lead: feasible on Windows CPU with the declared dependencies; use an in-process `llama-cpp-python` adapter, loopback-only host exposure, GGUF-only selection, and explicit concurrency control.
- Delivery manager: dependency order starts with `.gitignore`, config, downloader, health, and verification. Download size/time, HF availability, Windows paths, ignore coverage, and CPU/RAM limits are the main risks.
- Where reviewer recommendations conflicted with client answers, client answers take precedence. The service is not download-only; in-memory CPU inference is in scope.
- Caching completed responses was the original basis for unloading after each response. `DECISION-21` supersedes that immediate unload with a short idle-retention window; keeping loaded models as the response cache (instead of caching completed responses) is still rejected.
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
| `DECISION-9` | Models load only when their endpoint is called. (Immediate unload after response generation was superseded by `DECISION-21`.) | Client | Closed; unload timing superseded by `DECISION-21` |
| `DECISION-10` | Support both startup config-defined models and runtime admin endpoints. | Client | Closed |
| `DECISION-11` | Public Hugging Face models only; no gated/private repos in MVP. | Client | Closed |
| `DECISION-12` | Cache completed responses on disk, not loaded model instances. | Client | Closed |
| `DECISION-13` | Cache key is the full normalized model run request. | Client | Closed |
| `DECISION-14` | Cache files live in `.cache/responses` and are ignored by Git. | Client | Closed |
| `DECISION-15` | Streaming responses are cached after complete assembly and cache hits are replayed as SSE. True incremental generation on a cache miss is defined by `DECISION-23`. | Client | Closed; cache-miss behavior superseded by `DECISION-23` |
| `DECISION-16` | Default llama.cpp load settings are `n_ctx: 16384`, `n_batch: 512`, and `flash_attn: true`. | Client | Closed |
| `DECISION-17` | OpenCode uses `limit` singular and `body.options`; unsupported `limits`/top-level `options` keys are not used. | Client + Tech Lead | Closed with correction |
| `DECISION-18` | All development, testing, deployment, and other execution happens in Docker containers only. | Client | Closed |
| `DECISION-19` | Environments are created inside Docker with Miniconda from the committed linux-64 lock derived from `environment.yaml`. | Client | Closed |
| `DECISION-20` | Default sampling settings are `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`, sent through OpenCode `body.options`. | Client | Closed |
| `DECISION-21` | Keep a lazily loaded GGUF resident for `server.idle_unload_seconds` (default 180 seconds; validated `> 0` and `<= 86400` seconds / 24 hours) after the last prompt instead of unloading immediately after the response or SSE `[DONE]`. A prompt for the loaded alias and a cache hit reset the timer; a prompt for a different alias, explicit admin unload, or a runtime/cache-put error unloads immediately and cancels the timer. Preflight is skipped when reusing the already loaded alias; only one instance is resident at a time; the timer uses the same process-wide lock. | Client | Closed |
| `DECISION-22` | Close `SEC-G-01` by moving slow GGUF downloads outside the process-wide lifecycle lock. A dedicated per-alias download lock prevents duplicate same-alias downloads; the lifecycle lock still guards short existence re-checks and registry mutation. Startup downloads run concurrently up to `download.max_parallel_downloads` (default `4`; valid `> 0` and `<= 16`) after fail-closed preflight checks. One model instance, serialized generation, loopback-only publish, and idle retention remain unchanged. | Tech Lead + Client | Closed |
| `DECISION-23` | On a `stream: true` cache miss, generate with llama.cpp `stream=True` and emit each chunk as SSE as it arrives. Hold the lifecycle lock from pre-response setup through iteration; contention before the response still returns `503`. Assemble and cache one completed response only after success. Cache hits keep replaying stored responses; non-streaming behavior is unchanged. A generation/cache error after streaming starts closes the chunk iterator, unloads immediately, emits a sanitized SSE `error` event without `[DONE]`, and never caches partial content. Client disconnect is cleanup-only with no error frame. Usage is included only when supplied by runtime chunks; the installed llama-cpp-python has no `stream_options` parameter. | Client | Closed |
| `DECISION-24` | Make the untracked `private.txt` → `HF_TOKEN` injection optional for `api`, `tests`, and `dev`. When the file or token is absent, Compose and the service still start and public GGUF downloads proceed unauthenticated. When present, behavior and hygiene are unchanged: the token is never committed, copied into the image, passed as a CLI argument, or logged. Loopback-only publish, fail-closed preflight, `public_only`, the single-model lifecycle, and OpenCode V2/96-token settings remain unchanged. | Client | Closed |
| `DECISION-25` | Commit a linux-64-only explicit conda lock generated inside Docker from the current `local-llm` environment. The lock records exact package URLs with SHA-256 fragments; the Dockerfile pins the Miniconda base image by immutable digest, creates the environment from the lock, and removes the floating `pip install --upgrade pip`. `environment.yaml` remains the human-edited source of truth. Multi-architecture locks are deferred; the client confirmed linux-64 only. Runtime behavior, loopback-only publish, optional `HF_TOKEN`, and OpenCode V2/96-token settings are unchanged. | Tech Lead + Client | Closed |

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
- Keep the loaded GGUF resident for `server.idle_unload_seconds` (default 180 seconds; valid `> 0` and `<= 86400` / 24 hours) after the last prompt, then unload; a prompt for the loaded alias or a cache hit resets the timer.
- A prompt for a different alias unloads the current instance immediately before loading the new one; explicit admin unload and runtime/cache-put errors unload immediately and cancel the timer.
- Preflight is skipped when reusing the already loaded alias; only one model instance exists at a time; the idle timer uses the same process-wide lock.
- On-disk cache for completed chat responses, keyed by the full normalized request.
- Cache hit must return the cached response without loading a GGUF or running inference.
- Cache miss must store the completed response atomically after generation.
- SSE replay for cached responses when `stream: true`.
- Configurable cache TTL and maximum cache size.
- `llama_defaults` applied to every model load, with per-model overrides.
- Sampling defaults applied to every generation, with per-request overrides.
- OpenCode model entries use `limit` and `body.options`.
- Docker-only development, testing, deployment, and operations.
- Miniconda-based environment creation from the committed linux-64 lock derived from `environment.yaml`.
- Stable error envelope.
- Host-only loopback exposure through Docker port publishing.
- README configuration guide for OpenCode and Docker.

## Deferred

- Gated/private Hugging Face model support. `HF_TOKEN` is optional and is used only for faster, rate-limit-favored public downloads; public downloads work without it under `DECISION-24`.
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
- Multi-architecture conda locks beyond the committed linux-64 lock.

## Rejected

- 7B parameter enforcement.
- Committing model weights.
- Non-GGUF formats.
- Gated/private model support beyond public repos.
- Loading all registered models into memory at startup.
- Keeping loaded models resident between requests as a cache (using loaded instances as the response cache). Short idle retention under `DECISION-21` is not a cache and does not change what is cached.
- Host-local Python or conda execution for development, testing, deployment, or other tasks after `DECISION-18`.
- OpenCode key spellings `limits` and top-level `options`, because OpenCode V2 documents `limit` and provider options through `settings`/`headers`/`body`.

## Top-seven hardening plan

Status: **planning baseline committed and pushed; documentation/verification gates defined and verified**. Baseline: `3d7e43a Baseline local API and OpenCode configuration` was pushed to `origin/main` before this plan was written. Item 4's idle-retention behavior (`DECISION-21`) is implemented, documented, and verified by `AC-57`–`AC-61`.

Lead input: Tech Lead found items 1–7 already implemented and recommended a verification, security, and documentation pass rather than new application logic. Delivery Manager confirmed the shortest sequence is baseline → documented mitigations → Docker verification. Security Engineer identified the loopback trust boundary, plaintext cache data, local admin download surface, token-leakage footgun, root container, and lock contention as the reachable risks. This plan preserves the seven features and resolves those inputs conservatively: explicit mitigations and residual decisions in the docs, and verification through the Docker gates. Items 1–3 and 5–7 are documentation/verification-only; item 4 includes the approved idle-retention runtime change (`DECISION-21`) covered by `AC-57`–`AC-61`.

| Item | Feature preserved | Cybersecurity mitigation / boundary | Gate |
|---:|---|---|---|
| 1 | Full-request response cache with no bypass; TTL 30 days and 1 GiB cap | Treat `.cache/responses/` as sensitive response data; restrict filesystem access to the container/host owner; do not put secrets in prompts. The prompt participates in the SHA-256 key but is not stored verbatim. No cache bypass or encryption is added in this quick pass. | `AC-49` |
| 2 | Host loopback-only exposure without auth | Keep `127.0.0.1:8000` as the only host publish; never change it to `0.0.0.0`. Loopback is the trust boundary: any local host process is trusted. Auth, LAN binding, and external-IP request guards are rejected for this pass. | `AC-50` |
| 3 | Fail-closed RAM/disk preflight | Keep configurable thresholds, disk check before download, RAM check before load, and no model bytes/load on failure. Do not replace threshold failures with warnings. | `AC-51` |
| 4 | Lazy load on first call; keep the GGUF resident for `server.idle_unload_seconds` (default 180 s; valid `> 0` and `<= 86400` s / 24 h) after the last prompt, then unload | Keep the process-wide lock and `server.lock_timeout_seconds: 600`; contention stays `503`. The idle timer uses the same lock. A prompt for the loaded alias or a cache hit resets the timer; a different-alias prompt, explicit admin unload, or a runtime/cache-put error unloads immediately and cancels the timer. Preflight is skipped when reusing the already loaded alias; only one instance exists at a time; OpenCode's 96-token cap is unchanged. Serialized generation bounds memory but permits head-of-line blocking; concurrent generation and multi-model residency remain deferred. | `AC-52`, `AC-57`–`AC-61` |
| 5 | Docker-only execution | Use only `docker compose run --rm tests`, `docker compose run --rm dev`, and `docker compose --profile serve up -d api`. No host Python, host conda, or host uvicorn. Keep read-only config mounts. | `AC-53` |
| 6 | Optional `private.txt` → `HF_TOKEN` injection | Keep `env_file` for `api`, `tests`, and `dev` but mark it optional; keep the file gitignored/dockerignored; never copy it into the image, pass it as a CLI argument, or log it. Always use `--no-interpolate` when sharing Compose config. | `AC-54`, `AC-73`–`AC-76` |
| 7 | No 7B cap; user verifies model size | Keep config validation free of parameter-count rejection. Resource preflight remains the guardrail; documentation keeps size verification with the user. | `AC-55` |

Cross-cutting gate: `AC-56` verifies the baseline, tests, Compose config, and ignored state. Deferred product decisions, **not** implemented by this quick change: authentication/rate limiting/approved-alias gating for admin downloads, non-root container (Windows bind-mount permission impact), cache encryption or shorter TTL, content-hash GGUF fingerprints, multi-architecture dependency locks, and concurrent generation. These require product confirmation before scope expansion. Items 1–3 and 5–7 are documentation-only with observable gates `AC-49` through `AC-56`; the item-4 idle-retention change adds observable gates `AC-57` through `AC-61`, all verified by the final Docker test run and the three-prompt OpenCode smoke.

Follow-up hardening after the security report: `DECISION-22` closes `SEC-G-01` by keeping network/download I/O outside the lifecycle lock and by parallelizing startup downloads with a bounded worker count. This does not add concurrent generation, multi-model residency, admin gating, or a second resident GGUF. Its gates are `AC-62`–`AC-66`.

Reproducible dependency hardening after `DECISION-24`: `DECISION-25` commits a linux-64 SHA-256 explicit lock, pins the linux/amd64 Miniconda manifest by digest, makes every Compose service target `linux/amd64`, and removes the floating pip upgrade. It is a C11 build-boundary change only; application modules, runtime behavior, loopback exposure, and `ASSUMPTION-17` are unchanged. Its gates are `AC-77`–`AC-80`.

## Configuration recommendation

Use four configuration layers:

1. `local_llm.yaml` — source of truth for the local API server, model registry, llama.cpp defaults, resource thresholds, runtime options, and cache settings.
2. `private.txt` — optional, untracked `HF_TOKEN=<token>` file injected by Compose into `api`, `tests`, and `dev` for faster public downloads. Compose works without it; the token is not needed for gated/private model access and never enters application code or CLI arguments.
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
  # Cold CPU inference can hold the process-wide lock longer than 30 seconds.
  lock_timeout_seconds: 600
  # Keep a loaded GGUF resident for this many idle seconds after the last prompt.
  # A prompt for the loaded alias or a cache hit resets the timer.
  # Valid values are > 0 and <= 86400 (24 hours); default 180.
  idle_unload_seconds: 180

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
  command: hf
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

  - alias: qwen25-coder-05b
    enabled: true
    repo_id: Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF
    filename: qwen2.5-coder-0.5b-instruct-q4_k_m.gguf
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
- `server.idle_unload_seconds`: must be `> 0` and `<= 86400` (24 hours); default `180`.
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
hf download <repo_id> <filename> --revision <revision> --local-dir models/<alias>
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
4. for `stream: true`, replay the complete response as SSE frames followed by `[DONE]`;
5. refresh the idle-unload timer when a model instance is resident — a cache hit counts as activity for the loaded alias.

Cache misses must:

1. perform normal resource preflight;
2. load only the requested alias;
3. generate the response;
4. write the cache entry;
5. start or reset the idle-unload timer for the loaded alias instead of unloading at generation/SSE completion; unload only after `server.idle_unload_seconds` (default 180 seconds; valid `> 0` and `<= 86400` / 24 hours) with no further prompt for that alias, or immediately on a different-alias prompt, explicit admin unload, or a runtime/cache-put error.

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
            "output": 96
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
              "repeat_penalty": 1.1,
              "max_tokens": 96
            }
          }
        }
      }
    }
  }
}
```

OpenCode V2 uses `limit` singular, not `limits`. There is no documented top-level `options` field for providers or models; provider-package options are expressed through `settings`, `headers`, and `body`. Local llama.cpp runtime options are sent to this API through `body.options`.

Every configured local provider caps output at 96 tokens in both `limit.output` and `body.options.max_tokens`. The cap is unchanged by idle retention: a resident GGUF is reused within `server.idle_unload_seconds`, but cold loads and alias switches still run CPU inference, so the short cap prevents long generations from blocking subsequent requests.

The process-wide lifecycle lock waits `server.lock_timeout_seconds` (default `600` seconds). The longer wait replaced the earlier fixed 30-second timeout, which caused spurious `503` responses during cold CPU inference. The idle-unload timer uses this same lock; the timeout remains `600` seconds.

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

The image is built from a digest-pinned Miniconda base image and creates the `local-llm` environment from the committed linux-64 explicit lock. `environment.yaml` remains the human-edited source of truth; the lock is generated and verified inside Docker.

## Assumptions needing implementation decisions

These product-level assumptions were closed by the tech lead. The concrete decisions are recorded in `docs/architecture.md` under "Closed implementation assumptions"; closing a decision does not change component status.

| ID | Assumption | Owner | Decision |
| --- | --- | --- | --- |
| `ASSUMPTION-1` | Config lives in a repo-root YAML file with env overrides. | Tech Lead | Closed by ConfigLoader implementation. |
| `ASSUMPTION-2` | Inside Docker the app binds `0.0.0.0:8000`; Compose publishes only to host `127.0.0.1:8000`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-3` | Admin route names will be `POST /admin/models`, `POST /admin/models/{alias}/unload`, and `DELETE /admin/models/{alias}`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-4` | Resource thresholds are configurable with safe defaults. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-5` | Admin endpoints require no auth because the host publish is loopback-only. | Tech Lead + Security Engineer | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-6` | HF CLI invocation uses the configured CLI (`hf` by default) with `--local-dir`; revision/filename are passed when configured. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-7` | Generation requests are globally serialized through one process-wide lock because only one model instance is resident at a time; the idle-unload timer uses the same lock. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-8` | Guard the streaming path with `try/finally` and write the cache only after the full response is assembled; with `DECISION-21`, a successful response starts/resets the idle timer instead of unloading at the final token, while errors unload immediately. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-9` | Runtime-added aliases are ephemeral unless copied into YAML. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-10` | OpenCode integration uses one custom provider per alias. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-11` | Cache keys use canonical JSON plus SHA-256 as described above. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-12` | Default cache limits are 30 days and 1024 MB. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-13` | Cache-hit SSE replay reconstructs chunks from the stored complete response rather than storing raw chunk bytes. `DECISION-23` separately forwards native chunks on a cache miss. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-14` | The installed `llama-cpp-python` version supports `flash_attn`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-15` | OpenCode sends runtime llama options through `body.options`. | Tech Lead | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-16` | Miniconda base image plus `environment.yaml` is sufficient until a committed lock file is generated. | Tech Lead + Delivery Manager | Closed; see `docs/architecture.md`. |
| `ASSUMPTION-17` | For the top-seven hardening plan, loopback-only exposure, token hygiene, cache-retention guidance, and Docker-only execution are sufficient cybersecurity mitigations for a trusted single-user host. | Product Lead + Security Engineer | **Open:** if the host is shared or remotely reachable, auth/rate limiting, non-root container, and cache encryption become required follow-up scope. |

## Acceptance criteria

Each criterion has one observable behavior and an owning module.

1. **[config]** Given a config with at least one valid alias, repo ID, and GGUF filename, startup registers that alias in `/v1/models`.
2. **[config]** Given a missing required field or duplicate alias, startup fails with a message naming the invalid field or alias.
3. **[gitignore]** `.gitignore` contains `models/` and `.cache/`, and `git check-ignore models/ .cache/responses/` reports both paths as ignored. `docker compose --profile serve up -d api` was smoke-tested with placeholder GGUF files and `/health` returned `{"status":"ok"}` from inside the container.
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
19. **[runtime]** After a non-streaming response completes, the loaded alias remains in memory; it is unloaded only after `server.idle_unload_seconds` (default 180 seconds; valid `> 0` and `<= 86400` / 24 hours) elapse with no further prompt for that alias, or immediately on a different-alias prompt, explicit admin unload, or a runtime/cache-put error.
20. **[runtime]** After a streaming response emits `[DONE]`, the loaded alias stays resident under the same idle-unload rule as `AC-19`; `[DONE]` alone does not unload the model.
21. **[size-policy]** A model is never rejected solely because its parameter count is unknown or greater than 7B.
22. **[errors]** Invalid request bodies return `422` with the stable error envelope.
23. **[errors]** Hugging Face CLI or runtime failures return sanitized `502`/`504` errors without leaking stack traces or paths.
24. **[security]** The API container listens on `0.0.0.0:8000` internally and Compose publishes it only on host `127.0.0.1:8000`.
25. **[docs]** README shows how to set OpenCode’s custom provider `baseURL` to `http://127.0.0.1:8000/v1/models/{alias}`.
26. **[docs]** README states clearly that the user is responsible for verifying model size before loading.
27. **[tests]** Contract and integration tests using a fake Hugging Face CLI and fake Llama runtime pass inside the test container.
28. **[verification]** A real-model smoke test is either recorded as passing inside Docker or explicitly marked skipped with reason. **Status: passed inside Docker with token-authenticated downloads.** Evidence: `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` downloaded with `hf` while `HF_TOKEN` from `private.txt` was configured, API startup reached `/health`, `GET /v1/models` returned `200`, and `POST /v1/models/qwen25-coder-05b/chat/completions` with `what is your name, respond using minimal tokens?` returned `200` with content `I am Qwen.` (usage 39/5/44). A repeated request replayed the cached response; admin unload returned `200` and admin delete returned `204`. Weights and cache entries were deleted after the test.
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
41. **[docker]** `docker compose --profile serve up -d api` starts the API container with `models/` and `.cache/responses/` mounted as persistent volumes. **Status: verified with placeholder GGUF files (`/health` returned `{"status":"ok"}`) and with the real token-authenticated model smoke (all HTTP endpoints reachable from `127.0.0.1:8000`).**
42. **[docker]** No development or verification step in the runbook invokes host Python or host conda.
43. **[generation-config]** Given default OpenCode and YAML config, generation uses `temperature: 0.1`, `top_p: 0.2`, `top_k: 20`, `min_p: 0.08`, and `repeat_penalty: 1.1`.
44. **[security/token]** The optional, untracked `private.txt` file may supply `HF_TOKEN` to the `api`, `tests`, and `dev` Compose services through `env_file`; when absent, Compose and the service still start for public downloads. The token is never passed as a CLI argument, copied into the image, or logged. **Status: verified** by Compose config with and without the file, container-environment tests, and authenticated and unauthenticated real-model smokes.
45. **[tests]** A test captures the model output for the prompt `what is your name, respond using minimal tokens?`. **Status: passed** as a contract test with the injected fake runtime; the same prompt was also sent to the real Qwen model during the Docker smoke.
46. **[docker]** The API endpoints are reachable from the host loopback address: `GET /health`, `GET /v1/models`, and `POST /v1/models/{alias}/chat/completions`. **Status: verified** with the real-model smoke inside Docker.
47. **[config]** Given default config, the process-wide lifecycle lock waits `server.lock_timeout_seconds` seconds, and the value is configurable in `local_llm.yaml`. **Status: verified** by config and route contract tests.
48. **[opencode]** Every local OpenCode provider caps output at 96 tokens in both `limit.output` and `body.options.max_tokens`. **Status: verified** by OpenCode-config contract tests.
49. **[plan/cache]** This plan documents that the cache has no per-request bypass; an existing cache test repeats a normalized request without invoking the fake Llama runtime, and README states that `.cache/responses/` may contain response text and must be access-restricted with `ttl_days: 30` and `max_size_mb: 1024` as retention limits.
50. **[plan/security]** This plan documents that Compose publishes only `127.0.0.1:8000`, no auth is added, and any local host process is trusted; `docker compose --profile serve config --no-interpolate` shows `host_ip: 127.0.0.1` and no `0.0.0.0` host publish.
51. **[plan/preflight]** Existing preflight tests still pass and this plan documents that insufficient disk or RAM fails closed with `503`, writes no model bytes, and loads no model.
52. **[plan/runtime]** This plan documents lazy load with idle retention (`server.idle_unload_seconds: 180`, valid `> 0` and `<= 86400` / 24 hours) rather than unload at non-streaming completion or SSE `[DONE]`, with `server.lock_timeout_seconds: 600` and `503` on contention; the idle timer uses the same lock. Observable behavior is covered by `AC-19`, `AC-20`, and `AC-57`–`AC-61`.
53. **[plan/docker]** README and `AGENTS.md` show only Docker Compose commands for tests, dev shell, and deployment, with no host Python, host conda, or host uvicorn command.
54. **[plan/token]** `docker compose --profile serve config --no-interpolate` shows an optional `env_file` (`required: false`) for `api`, `tests`, and `dev`; `git check-ignore -v private.txt` passes; docs require `--no-interpolate`, and no token value appears in logs, CLI arguments, or the image.
55. **[plan/size]** This plan documents that no model is rejected solely for unknown or greater-than-7B parameter count, the user verifies size before load, and RAM/disk preflight remains the only automated guardrail.
56. **[plan/verification]** Baseline commit `3d7e43a` was pushed to `origin/main` before this plan; `docker compose run --rm tests` passes all tests (baseline 95; final 110 after `DECISION-21`); `docker compose --profile serve config --no-interpolate` succeeds; `git check-ignore -v private.txt models/ .cache/responses/` passes; no weights or cache files are staged; requirements, architecture, API runbook, and README are updated together.

The following criteria document the idle-retention change (`DECISION-21`) and are **verified** by the final Docker test run and the three-prompt OpenCode end-to-end smoke:

57. **[runtime/idle]** Given `server.idle_unload_seconds` (default 180 seconds; valid `> 0` and `<= 86400` / 24 hours), a successful chat response leaves the loaded alias resident, and the instance is unloaded only after that many idle seconds elapse with no further prompt for the alias.
58. **[runtime/idle]** A prompt for the loaded alias and a cache hit each reset the idle timer. A cache hit does not load a model or run inference, and preflight is skipped when reusing the already loaded alias.
59. **[runtime/idle]** A prompt for a different alias unloads the resident instance immediately before loading the new alias, and only one model instance exists at a time.
60. **[runtime/idle]** Explicit `POST /admin/models/{alias}/unload` cancels the idle timer, and runtime or cache-put errors unload immediately and cancel the timer. The timer uses the same process-wide lock (`server.lock_timeout_seconds: 600`), and the OpenCode 96-token cap is unchanged.
61. **[end-to-end]** With the API served from Docker and the configured provider `local-qwen-coder-05b/chat`, a fresh OpenCode session sends one prompt and two follow-up prompts in that same session; all three inputs produce non-empty assistant outputs from the local endpoint, and the local provider still uses the supported V2 fields and 96-token caps.

The following criteria document the download-hardening change (`DECISION-22`, closing `SEC-G-01`):

62. **[download/lock]** When a chat request restores a missing GGUF, `downloader.download()` runs without holding the process-wide lifecycle lock; the lifecycle lock is re-acquired only for the existence re-check and `mark_downloaded()`. Contention and preflight failures still map to stable `503` envelopes.
63. **[download/concurrency]** Concurrent chat requests for the same missing alias execute at most one download; the per-alias download lock serializes that alias while different aliases can download in parallel.
64. **[startup/parallel]** Enabled configured models are downloaded concurrently at startup up to `download.max_parallel_downloads` (default `4`; valid `> 0` and `<= 16`), and all missing models reach `DOWNLOADED` registry state without loading a model.
65. **[startup/preflight]** A failing disk preflight stops startup before any configured download command runs.
66. **[security/verification]** `SEC-G-01` is marked mitigated in `docs/security-report.md`; `docker compose run --rm tests` passes the full suite in Docker, Compose config still publishes only `127.0.0.1:8000`, and no model weights or cache files are staged or committed.

The following criteria document the true-streaming change (`DECISION-23`):

67. **[runtime/stream]** On a cache miss with `stream: true`, the runtime calls `create_chat_completion(..., stream=True)` with merged sampling options and forwards llama.cpp chunks as they are produced rather than generating a complete response first.
68. **[routes/stream]** The route emits incremental `chat.completion.chunk` SSE frames, including separate content deltas, before a final `[DONE]`; the assembled OpenAI-compatible completed response is cached only after successful exhaustion of the stream.
69. **[stream/cache]** A subsequent equivalent non-streaming request returns the assembled cached response without another inference call. Cache hits for streaming requests still replay the stored response, and `stream` remains excluded from the cache key.
70. **[stream/lifecycle]** The lifecycle lock is acquired before the streaming response is returned, remains held during chunk iteration, and is released after success, error, or early generator close. Pre-stream contention still returns stable `503`; successful completion schedules the same idle-unload timer as non-streaming generation.
71. **[stream/errors]** A generation or cache-write error after streaming starts unloads the model immediately, cancels idle retention, emits a sanitized SSE `error` event without `[DONE]`, writes no partial cache entry, and exposes no prompt, path, stack trace, token, or raw exception text. Early generator close also releases the lock and writes no partial cache entry.
72. **[stream/verification]** Non-streaming tests remain green; a real-model Docker streaming smoke emits multiple incremental content frames and `[DONE]`, an equivalent cached request requires no new inference, OpenCode V2/96-token configuration still validates, Compose remains loopback-only, and no weights or cache files are staged or committed.

The following criteria document the optional-token change (`DECISION-24`):

73. **[compose/token]** The `private.txt` `env_file` entries for `api`, `tests`, and `dev` are declared with `required: false`; `docker compose --profile serve config --no-interpolate` and `docker compose run --rm tests` both succeed when `private.txt` is absent.
74. **[download/token]** With no `HF_TOKEN` in the container environment, startup still downloads public GGUFs through the same `hf` CLI invocation, the API reaches `/health`, and no token argument is added to the CLI command.
75. **[token/hygiene]** When `HF_TOKEN` is present it is injected only from the ignored `private.txt`; it is never copied into the image, passed as a CLI argument, or logged. Container tests accept an absent token but validate the shape of a present token.
76. **[verification]** Public download behavior, fail-closed preflight, `public_only`, loopback-only publish, single-model lifecycle, OpenCode V2/96-token settings, and ignored model/cache paths are unchanged; documentation and tests are updated together.

The following criteria document the reproducible dependency lock (`DECISION-25`):

77. **[lock/source]** `environment.yaml` remains the human-edited dependency source, and `environment-linux-64.lock` is a committed linux-64 explicit lock generated inside Docker from the `local-llm` environment. Tests assert that the lock declares `@EXPLICIT`, `platform: linux-64`, exact package URLs with 64-hex SHA-256 fragments, and every dependency declared in `environment.yaml`; regenerating the lock from the installed container environment reproduces the committed file byte-for-byte.
78. **[build/digest]** The Dockerfile pins the Miniconda base image by the immutable linux/amd64 manifest digest, installs `local-llm` with `conda create --name ... --file environment-linux-64.lock`, and contains neither `conda env create -f environment.yaml` nor an unpinned `pip install --upgrade pip`. Every Compose service declares `platform: linux/amd64` so the lock and image architecture agree.
79. **[runtime/lock]** A fresh image built solely from the committed lock passes the full container test suite. Container tests assert that installed versions of `python`, `huggingface_hub`, and `llama-cpp-python` match the lock and that the `hf` entry point exists.
80. **[verification]** The lock change is isolated to C11 DockerRuntime: application modules, loopback-only publish, optional `HF_TOKEN`, `public_only`, fail-closed preflight, single-model lifecycle, OpenCode V2/96-token settings, and ignored model/cache paths remain unchanged; documentation and tests are updated together. Multi-architecture locks remain deferred.

`DECISION-25` verification: `docker buildx imagetools inspect continuumio/miniconda3:24.9.2-0@sha256:39b820af05f0f9bb986ca145a0d077bbd3bd866256277dd869fefb93bd03cd21` identifies the pinned manifest as `linux/amd64`; `docker compose build --no-cache api tests dev` succeeded from that digest using only `environment-linux-64.lock`; `conda list -n local-llm --explicit --sha256` in the rebuilt dev container reproduced the committed lock byte-for-byte; and the final `docker compose run --rm tests` run completed with `Ran 142 tests ... OK`. Compose config remains loopback-only with optional token injection.

## Delivery sequence

1. Add `models/` and `.cache/` to `.gitignore`.
2. Add Docker-only artifacts: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and Miniconda environment creation.
3. Define config schema and validation, including llama defaults, runtime options, and cache settings.
4. Implement Hugging Face CLI downloader.
5. Implement model registry and `/v1/models`.
6. Implement admin add/unload/remove endpoints.
7. Implement configurable resource preflight.
8. Implement lazy runtime adapter and per-model chat routes with merged llama options.
9. Implement SSE streaming plus idle-unload cleanup (`DECISION-21`), then native incremental generation on streaming cache misses (`DECISION-23`).
10. Implement response cache keying, storage, lookup, replay, expiry, and eviction.
11. Add error envelope, host loopback Docker publishing, and cache/runtime tests.
12. Write README and OpenCode configuration example using `limit` and `body.options`.

## Next step

MVP implementation is complete, including the C1 production composition root and ASGI entry point; `ASSUMPTION-1` through `ASSUMPTION-16` are closed and recorded in `docs/architecture.md`. `DECISION-21` adds idle retention for the lazily loaded GGUF (`server.idle_unload_seconds` defaults to 180 seconds and is validated `> 0` and `<= 86400` / 24 hours); its observable behavior is recorded in `AC-19`, `AC-20`, and `AC-57`–`AC-61`, all verified by the final Docker test run and the three-prompt OpenCode smoke. The top-seven hardening plan is otherwise documentation/verification-only through `AC-49`–`AC-56`; `DECISION-22` covers download hardening, `DECISION-23` adds true incremental streaming on cache misses with completed-response caching and sanitized stream-error cleanup, and `DECISION-24` makes `HF_TOKEN` optional without weakening token hygiene; `DECISION-25` commits a linux-64 explicit dependency lock and digest-pins the Miniconda base image. `ASSUMPTION-17` remains open and must be confirmed before adding auth, rate limiting, non-root container, or cache-encryption scope. All verification after this point must be executed inside Docker containers.
