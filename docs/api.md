# API operator runbook

Status: **implemented HTTP contract; external boundaries exercised by a token-authenticated real-model smoke test**. Request/response shapes and status codes below are derived from the route code (`local_llm/chat_routes.py`, `local_llm/admin_routes.py`), error mapping (`local_llm/error_mapper.py`, `local_llm/composition.py`), and Compose config (`docker-compose.yml`). Contract tests use a fake Hugging Face CLI and a fake Llama runtime. A real `hf` download and real GGUF CPU inference were verified inside Docker with `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` using `HF_TOKEN` injected from `private.txt`; the weights and cache entries were removed afterward.

Authoritative scope: `docs/requirements.md`. Component/design detail: `docs/architecture.md`. Runtime settings: `local_llm.yaml`.

## Docker-only workflow

All development, testing, deployment, and verification run inside Docker. Do not use host Python or host conda. From the repository root:

```powershell
# 1. Validate the serve profile before deploying.
#    Check the api command, loopback-only publish, env_file, and read-only config mount.
#    Use --no-interpolate so the resolved HF_TOKEN is not printed into shared logs.
docker compose --profile serve config --no-interpolate

# 2. Start the API in the background.
docker compose --profile serve up -d api

# 3. Follow logs (Ctrl+C to detach, container keeps running).
#    Startup status lines show downloading model, download complete, or already present.
docker compose --profile serve logs -f api

# 4. Stop and remove the API container; bind-mounted models/cache data persists.
docker compose --profile serve down

# Run the full test suite; open an interactive development shell.
docker compose run --rm tests
docker compose run --rm dev
```

The API container runs `uvicorn local_llm.api:app --host 0.0.0.0 --port 8000`. The container binds `0.0.0.0:8000` internally, but Compose publishes only `127.0.0.1:8000` on the host — the service is loopback-only by design. Health check: `curl http://127.0.0.1:8000/health`.

Mounts and persistence:

| Path | Mount | Git |
| --- | --- | --- |
| `models/` | bind-mounted into the container; GGUF weights land in `models/<alias>/` | gitignored — never commit weights |
| `.cache/` | bind-mounted; completed responses in `.cache/responses/` | gitignored — never commit cache files |
| `local_llm.yaml` | mounted read-only (`:ro`) | committed config |
| `private.txt` | read by Compose as `env_file`; not copied into the image | gitignored — never commit the token |

Create `private.txt` at the repository root with one line before running Compose:

```text
HF_TOKEN=<token>
```

`docker-compose.yml` injects that value as `HF_TOKEN` into the `api`, `tests`, and `dev` services. The file is required, so Compose fails fast when it is missing. `hf` and `huggingface_hub` read the variable automatically for authenticated, rate-limit-favored public downloads. The token is never passed as a CLI argument, baked into the image, or written to logs.

Enabled models from `local_llm.yaml` are downloaded at startup by the app lifespan. A missing GGUF on a later request is restored on demand (see chat completions below).

## Stable error envelope

Application-level failures (domain exceptions, request validation, and unexpected exceptions) use one sanitized shape. The `type` value is the Python exception class name; internal paths and stack traces are not exposed. Framework-level responses such as an unknown route are not produced by these handlers.

```json
{
  "error": {
    "code": "resource_unavailable",
    "message": "Required resources are unavailable.",
    "type": "ResourceError"
  }
}
```

| Status | `code` | When |
| --- | --- | --- |
| 404 | `not_found` | Unknown alias (`UnknownAliasError`, `KeyError`) |
| 409 | `conflict` | Duplicate alias (`DuplicateAliasError`) |
| 422 | `validation_error` | Invalid request body (`RequestValidationError`, `ValidationError`, `ValueError`) |
| 503 | `resource_unavailable` | Fail-closed disk/RAM preflight, or process lock contention (`ResourceError`) |
| 502 | `download_error` / `runtime_error` | Download failure (`DownloaderError`) / runtime failure (`RuntimeError`) |
| 504 | `download_timeout` / `runtime_timeout` | Download timeout (`DownloadTimeoutError`) / generation timeout (`TimeoutError`) |
| 500 | `internal_error` | Unmapped unexpected failure |

The process-wide model lock has a 30-second timeout; failing to acquire it returns `503 resource_unavailable`.

## Endpoint reference

### `GET /health`

Purpose: liveness probe. Does not load a model or start a download.

- Parameters/body: none.
- Success: `200` with `{"status": "ok"}`.
- Errors: no endpoint-specific errors; unexpected failures map to `500`.
- Side effects: none.

### `GET /v1/models`

Purpose: list registered aliases in OpenAI-compatible model-list shape.

- Parameters/body: none.
- Success: `200` (abbreviated example):

```json
{
  "object": "list",
  "data": [
    {"id": "phi4-mini", "object": "model", "created": 0, "owned_by": "local"},
    {"id": "qwen25-coder-3b", "object": "model", "created": 0, "owned_by": "local"}
  ]
}
```

- Errors: no endpoint-specific errors; unexpected failures map to `500`.
- Side effects: none. `id` is the alias; internal filesystem paths are never serialized here.

### `POST /v1/models/{alias}/chat/completions`

Purpose: OpenAI-compatible chat completion (JSON or SSE) for one registered alias. The alias is loaded only for this request and unloaded after the response.

- Path parameter: `alias` (string; must already be registered).
- Request body: a `ChatCompletionRequest` object.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `messages` | `list[dict]` | **yes** | OpenAI-style message objects; e.g. `{"role": "user", "content": "hi"}` |
| `model` | `string \| null` | no | Default `null` |
| `stream` | `boolean` | no | Default `false`. Excluded from the cache key. |
| `temperature`, `top_p`, `min_p` | `number \| null` | no | Sampling overrides |
| `top_k`, `n`, `seed`, `max_tokens` | `integer \| null` | no | Sampling overrides |
| `repeat_penalty`, `frequency_penalty`, `presence_penalty` | `number \| null` | no | Sampling overrides |
| `stop` | any | no | Default `null` |
| `options` | `object` | no | Default `{}`; keys merged as described below |

Minimal JSON request:

```json
{
  "messages": [{"role": "user", "content": "hello"}]
}
```

Minimal JSON response (`200`, `application/json`). This is the OpenAI `chat.completion` shape; concrete values come from llama.cpp at runtime (the example matches the contract-test response shape):

```json
{
  "id": "chatcmpl-test",
  "object": "chat.completion",
  "created": 1,
  "model": "alias-a",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
}
```

SSE request/response: add `"stream": true`. The response is `200` with `Content-Type: text/event-stream`. Frames are `data: <json>\n\n` objects of type `chat.completion.chunk`, ending with `data: [DONE]\n\n`:

```json
{"messages": [{"role": "user", "content": "hello"}], "stream": true}
```

```text
data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"alias-a","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"alias-a","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"alias-a","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"alias-a","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}

data: [DONE]
```

SSE frames are reconstructed by replaying the stored complete response, not produced by true incremental generation (this is by design). On a cache miss the model runs with `stream` forced to `false` and the full response is cached before replay.

Notable errors: `404 not_found` (unknown alias), `422 validation_error` (invalid body, e.g. missing `messages`), `503 resource_unavailable` (load preflight or lock contention), `502 runtime_error` / `502 download_error`, `504 runtime_timeout` / `504 download_timeout`, `500 internal_error`.

Side effects and ordering:

1. Resolve the GGUF. If the registered file is missing, acquire the lock, re-check, run fail-closed download preflight, download into `models/<alias>/`, and mark downloaded — this happens **before** the cache key is computed.
2. Compute the cache key and check the cache. On a hit, replay the response (SSE if `stream: true`) with **no** load, preflight-load, or inference.
3. On a miss: acquire the lock, re-check cache, run fail-closed load preflight, load the model with merged load options, mark the alias `LOADED`, generate with `stream: false`, write the completed response to cache, then unload and mark `UNLOADED` in a `finally` block. Errors are not cached; unload still runs on failure.

Option merge order (later sources win):

- Load options (`n_ctx`, `n_batch`, `flash_attn`, `n_threads`, `n_gpu_layers`, `verbose`): `llama_defaults` → model `options` → request `options`.
- Sampling options (`temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`, `max_tokens`, `frequency_penalty`, `presence_penalty`, `seed`, `n`, `stop`): `generation_defaults` → model sampling `options` → request top-level fields → request `options`.

An annotated request showing where values come from (JSONC for comments only; send plain JSON):

```jsonc
{
  "messages": [{"role": "user", "content": "hello"}],
  "temperature": 0.7,              // request top-level sampling field
  "options": {                     // per-request overrides, merged last
    "n_ctx": 8192,                 // load option
    "flash_attn": false,           // load option
    "min_p": 0.05                  // sampling option
  }
}
```

Cache key inputs include the model `alias`, a schema version, normalized `messages`, normalized request params (sampling fields, `model`, etc.), merged runtime load/sampling options, model `revision`, and the GGUF weight fingerprint (`size:mtime_ns`). `stream`, `stream_options`, and `request_id` are excluded; the latter two are not declared on the request model, so they never reach the key. `make_cache_key` drops a top-level `verbose` key from its `runtime` mapping, but the chat flow nests load options under `load`, so `verbose` from defaults, model options, or request options is included in the key. Consequently, streaming and non-streaming requests share cache entries, and changing messages, sampling/runtime options, alias, revision, or weight bytes produces a different key.

Runtime-added aliases are ephemeral: the server never rewrites `local_llm.yaml`. Copy a desired entry into the YAML to make it permanent.

### `POST /admin/models`

Purpose: download a public GGUF and register a new alias at runtime.

- Path parameters: none.
- Request body: a `ModelRecord` object.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `alias` | `string` | **yes** | Must match `^[a-z0-9_-]+$` |
| `repo_id` | `string` | **yes** | Public repo, `owner/name` |
| `filename` | `string` | **yes** | `.gguf` basename (no path separators) |
| `revision` | `string` | no | Default `main` |
| `enabled` | `boolean` | no | Default `true` |
| `options` | `object` | no | Default `{}`; keys restricted to the supported llama/sampling option names |

```json
{
  "alias": "alias-b",
  "repo_id": "owner/model-b",
  "filename": "model-b.gguf",
  "revision": "main",
  "options": {"n_ctx": 8192, "temperature": 0.5}
}
```

- Success: `201` with the registered record (`model_dump(mode="json")`), e.g.:

```json
{
  "alias": "alias-b",
  "enabled": true,
  "repo_id": "owner/model-b",
  "filename": "model-b.gguf",
  "revision": "main",
  "options": {"n_ctx": 8192, "temperature": 0.5}
}
```

No filesystem path is returned.

- Errors: `409 conflict` (duplicate alias, no download attempted), `503 resource_unavailable` (disk preflight failed; no file written and no registry change), `422 validation_error` (bad alias/repo/filename/options), `502 download_error`, `504 download_timeout`, `500 internal_error`.
- Side effects: fail-closed download preflight, download into `models/<alias>/`, add the alias to the registry, mark it `DOWNLOADED`. The alias is ephemeral; `local_llm.yaml` is not modified.

### `POST /admin/models/{alias}/unload`

Purpose: unload the alias's in-memory model instance; a no-op when it is not loaded.

- Path parameter: `alias` (string; must be registered).
- Request body: none.
- Success: `200`:

```json
{"alias": "alias-a", "unloaded": true}
```

When not loaded, returns `200` with `{"alias": "alias-a", "unloaded": false}`.

- Errors: `404 not_found` (unknown alias), `503 resource_unavailable` (lock contention), `500 internal_error`.
- Side effects: under the process lock, if the registry state is `LOADED` and the runtime holds an instance, unload the runtime and set state `UNLOADED`. The GGUF file and cache are untouched.

### `DELETE /admin/models/{alias}`

Purpose: unregister an alias without deleting its weights or changing the running runtime.

- Path parameter: `alias` (string; must be registered).
- Request body: none.
- Success: `204` with an empty body.
- Errors: `404 not_found` (unknown alias), `500 internal_error`.
- Side effects: registry removal only. Does **not** unload the runtime, delete the GGUF under `models/<alias>/`, or rewrite `local_llm.yaml`. Subsequent chat calls for that alias return `404`; runtime-added aliases can be re-added with `POST /admin/models`.

## Verification status

- Verified inside Docker: all 91 contract tests via `docker compose run --rm tests`, including every route above, cache hit/miss, SSE replay, 404/409/422/503/502 mappings, lazy load/unload, the container-environment token check, and capture of the model output for `what is your name, respond using minimal tokens?`. `docker compose --profile serve config --no-interpolate` confirms the loopback-only publish, `env_file`, and read-only config mount.
- Verified: a token-authenticated real-model smoke test inside Docker downloaded `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF` with `hf` without the unauthenticated warning. From host loopback, `GET /health`, `GET /v1/models`, and `POST /v1/models/qwen25-coder-05b/chat/completions` returned `200`; the prompt `what is your name, respond using minimal tokens?` produced `I am Qwen.`. A repeat replayed the cached response, admin unload returned `200`, and admin delete returned `204`. Weights and cache entries were removed afterward. Contract tests continue to use fakes.
