# Architecture

Status: **planned system architecture with implemented pieces marked**. This document describes the intended component boundaries for the local LLM API. It does not claim the full API is implemented.

Authoritative scope: `docs/requirements.md`. Runtime settings: `local_llm.yaml`. Operating rules: `AGENTS.md`.

## Component count

There are **14 modeled components**:

- **10 application components** that will live in the Python service.
- **4 boundary components** that are external systems or container/client configuration.

| ID | Component | Kind | Status | Responsibility |
| --- | --- | --- | --- | --- |
| C1 | CompositionRoot | Application | Planned | Constructs concrete settings, clients, stores, clocks, and runtimes; owns startup wiring. |
| C2 | ConfigLoader | Application | Planned | Reads `local_llm.yaml`, validates fields, merges defaults, and produces immutable settings. |
| C3 | ModelRegistry | Application | Planned | Tracks aliases, repo IDs, filenames, revisions, options, and registered/downloaded state. |
| C4 | HfCliDownloader | Application | Planned | Invokes `huggingface-cli download`, writes into `models/<alias>/`, and verifies the GGUF file. |
| C5 | ResourcePreflight | Application | Planned | Checks disk and memory estimates and fails closed before download or load. |
| C6 | LlamaRuntimeAdapter | Application | Planned | Loads a GGUF with merged llama options, generates completions, and unloads after response. |
| C7 | ChatRoutes | Application | Planned | Exposes `/health`, `/v1/models`, and `/v1/models/{alias}/chat/completions`. |
| C8 | AdminRoutes | Application | Planned | Exposes runtime add, unload, and remove operations. |
| C9 | ResponseCache | Application | Implemented | Stores completed responses, keys full normalized requests, evicts by TTL/size, and replays SSE. |
| C10 | ErrorMapper | Application | Planned | Converts config, download, preflight, runtime, and validation failures into stable HTTP errors. |
| C11 | DockerRuntime | Boundary | Implemented as files | Builds and runs the Miniconda image; publishes container port only on host loopback. |
| C12 | OpenCodeClient | Boundary | Implemented as config | Calls each alias-specific OpenAI-compatible endpoint using `limit` and `body.options`. |
| C13 | HuggingFaceCli | Boundary | External | Performs public model downloads inside the container. |
| C14 | LlamaCppLibrary | Boundary | External dependency | Provides `Llama(...)`, `create_chat_completion`, and streaming generation on CPU. |

## UML component view

```mermaid
flowchart LR
    subgraph Client["Client boundary"]
        OC["C12 OpenCodeClient"]
    end

    subgraph Container["Docker container - C11 DockerRuntime"]
        subgraph App["Python application"]
            ROOT["C1 CompositionRoot"]
            CFG["C2 ConfigLoader"]
            REG["C3 ModelRegistry"]
            DL["C4 HfCliDownloader"]
            PRE["C5 ResourcePreflight"]
            RT["C6 LlamaRuntimeAdapter"]
            CHAT["C7 ChatRoutes"]
            ADMIN["C8 AdminRoutes"]
            CACHE["C9 ResponseCache"]
            ERR["C10 ErrorMapper"]
        end

        HFCLI["C13 HuggingFaceCli"]
        LLAMA["C14 LlamaCppLibrary"]
        MODELS[("models/")]
        CACHEVOL[(".cache/responses/")]
    end

    OC -->|"127.0.0.1:8000/v1/models/{alias}/chat/completions"| CHAT
    ROOT --> CFG
    ROOT --> REG
    ROOT --> DL
    ROOT --> PRE
    ROOT --> RT
    ROOT --> CACHE
    ROOT --> ERR

    CFG -->|"settings and llama_defaults"| REG
    CFG -->|"thresholds and options"| PRE
    CFG -->|"cache settings"| CACHE

    CHAT --> CACHE
    CHAT --> PRE
    CHAT --> REG
    CHAT --> RT
    CHAT --> ERR

    ADMIN --> REG
    ADMIN --> DL
    ADMIN --> PRE
    ADMIN --> ERR

    DL --> HFCLI
    HFCLI --> MODELS
    REG --> MODELS
    RT --> LLAMA
    LLAMA --> MODELS
    CACHE --> CACHEVOL
```

## UML class view

```mermaid
classDiagram
    class CompositionRoot {
        +build() App
    }

    class ConfigLoader {
        +load(path) Settings
        +validate(settings) void
    }

    class Settings {
        +server
        +paths
        +llama_defaults
        +generation_defaults
        +cache
        +download
        +resources
        +models
    }

    class ModelRegistry {
        +list() list[ModelRecord]
        +get(alias) ModelRecord
        +add(record) void
        +remove(alias) void
        +markDownloaded(alias, path) void
    }

    class ModelRecord {
        +alias
        +repo_id
        +filename
        +revision
        +options
        +state
    }

    class HfCliDownloader {
        +download(record, destination) Path
        +exists(record, destination) bool
    }

    class ResourcePreflight {
        +checkDownload(destination) void
        +checkLoad(model_path) void
    }

    class LlamaRuntimeAdapter {
        +load(model_path, options) void
        +chat(request) Response
        +stream_chat(request) Iterator
        +unload() void
    }

    class ChatRoutes {
        +health() HealthResponse
        +models() ModelList
        +chat(alias, request) Response
    }

    class AdminRoutes {
        +add_model(request) ModelRecord
        +unload(alias) void
        +remove(alias) void
    }

    class ResponseCache {
        +get(key) Response
        +put(key, alias, revision, response) bool
        +replay_sse(response) Iterator
        +evict() int
    }

    class ErrorMapper {
        +map(exception) HTTPError
    }

    CompositionRoot --> ConfigLoader
    CompositionRoot --> ModelRegistry
    CompositionRoot --> HfCliDownloader
    CompositionRoot --> ResourcePreflight
    CompositionRoot --> LlamaRuntimeAdapter
    CompositionRoot --> ResponseCache
    CompositionRoot --> ErrorMapper
    ConfigLoader --> Settings
    ModelRegistry --> ModelRecord
    ChatRoutes --> ModelRegistry
    ChatRoutes --> ResponseCache
    ChatRoutes --> ResourcePreflight
    ChatRoutes --> LlamaRuntimeAdapter
    ChatRoutes --> ErrorMapper
    AdminRoutes --> ModelRegistry
    AdminRoutes --> HfCliDownloader
    AdminRoutes --> ResourcePreflight
```

## How the components do their work

| Component | Input | Work | Output | Failure behavior |
| --- | --- | --- | --- | --- |
| C1 CompositionRoot | Settings from C2 | Constructs all concrete components and injects collaborators. | Running FastAPI app. | Startup aborts with a named configuration error. |
| C2 ConfigLoader | `local_llm.yaml`, env overrides | Validates aliases, repo IDs, `.gguf` filenames, llama options, cache settings, and thresholds. | Immutable settings. | Raises validation error naming the offending field. |
| C3 ModelRegistry | Configured or runtime-added model records | Stores alias-to-model mappings and lifecycle state. | Model records for routes and runtime. | Duplicate or unknown aliases are rejected. |
| C4 HfCliDownloader | Model record and destination | Runs `huggingface-cli download` inside the container and verifies the file. | Local GGUF path. | Returns sanitized download error; writes no partial model path on rejected requests. |
| C5 ResourcePreflight | Disk/RAM thresholds and model path | Estimates required disk before download and memory before load. | Approval to proceed. | Fails closed with `503` and does not download or load. |
| C6 LlamaRuntimeAdapter | GGUF path, merged llama and sampling options, chat request | Calls `Llama(...)`, generates a response using merged `temperature`, `top_p`, `top_k`, `min_p`, and `repeat_penalty`, then unloads after completion. | OpenAI-compatible response or SSE stream. | Runtime failures are mapped to sanitized `502`/`504`. |
| C7 ChatRoutes | OpenAI-compatible request body | Checks cache first; on miss, coordinates preflight, registry, runtime, cache write, and unload. | JSON or SSE response. | Invalid bodies return `422` through C10. |
| C8 AdminRoutes | Add/unload/remove requests | Registers new models, unloads current instances, and removes aliases. | Updated registry state. | Invalid aliases or failed downloads return stable errors. |
| C9 ResponseCache | Cache key and completed response | Hashes canonical request data, writes atomically, expires and evicts entries, replays SSE. | Cached response or cache miss. | Corrupt or expired entries are deleted and treated as misses. |
| C10 ErrorMapper | Domain and runtime exceptions | Converts exceptions to stable HTTP status codes and sanitized envelopes. | HTTP error response. | Preserves safe cause metadata without leaking paths or stack traces. |
| C11 DockerRuntime | Dockerfile, Compose file, volumes | Builds Miniconda image, mounts `models/` and `.cache/`, publishes only host loopback. | Containerized test/dev/API environments. | Invalid Compose config fails before deployment. |
| C12 OpenCodeClient | `opencode.jsonc` providers | Sends chat requests to alias-specific base URLs with `limit` and `body.options`. | Selectable local models. | Missing server appears as provider/model unavailable. |
| C13 HuggingFaceCli | Public repo ID, filename, revision | Downloads exact GGUF file to `models/<alias>/`. | Files on mounted volume. | Auth, gated, or network failures propagate to C4. |
| C14 LlamaCppLibrary | GGUF path and llama options | Performs CPU inference and streaming generation. | Completion chunks or full response. | Load or generation errors propagate to C6. |

## UML sequence: cache hit

```mermaid
sequenceDiagram
    participant OC as C12 OpenCodeClient
    participant CHAT as C7 ChatRoutes
    participant CACHE as C9 ResponseCache

    OC->>CHAT: POST /v1/models/{alias}/chat/completions
    CHAT->>CHAT: build normalized cache key
    CHAT->>CACHE: get(key)
    CACHE-->>CHAT: cached response
    CHAT->>CACHE: replay_sse(response) if stream:true
    CACHE-->>OC: SSE frames + [DONE]
    Note over CHAT,CACHE: No preflight, no model load, no inference
```

## UML sequence: cache miss with model load and unload

```mermaid
sequenceDiagram
    participant OC as C12 OpenCodeClient
    participant CHAT as C7 ChatRoutes
    participant CACHE as C9 ResponseCache
    participant PRE as C5 ResourcePreflight
    participant REG as C3 ModelRegistry
    participant RT as C6 LlamaRuntimeAdapter
    participant LLAMA as C14 LlamaCppLibrary

    OC->>CHAT: POST /v1/models/{alias}/chat/completions
    CHAT->>CACHE: get(key)
    CACHE-->>CHAT: null
    CHAT->>PRE: checkLoad(model_path)
    PRE-->>CHAT: ok
    CHAT->>REG: get(alias)
    REG-->>CHAT: model record
    CHAT->>RT: load(path, merged options)
    RT->>LLAMA: Llama(n_ctx, n_batch, flash_attn, ...)
    CHAT->>RT: chat(request)
    RT-->>CHAT: completed response
    CHAT->>CACHE: put(key, alias, revision, response)
    CHAT->>RT: unload()
    CHAT-->>OC: JSON or SSE response
```

## UML state: model lifecycle

```mermaid
stateDiagram-v2
    [*] --> Registered
    Registered --> Downloaded: startup or admin add
    Downloaded --> Unloaded: ready
    Unloaded --> Loading: first endpoint call
    Loading --> Loaded: Llama created
    Loaded --> Generating: chat request
    Generating --> Unloaded: response complete or [DONE]
    Generating --> Failed: runtime error
    Failed --> Unloaded: cleanup
    Registered --> Removed: admin remove
    Downloaded --> Removed: admin remove
    Unloaded --> Removed: admin remove
    Removed --> [*]
```

## UML deployment view

```mermaid
flowchart TB
    Host["Host machine"]
    Loop["127.0.0.1:8000"]
    Container["Docker container"]
    Uvicorn["uvicorn local_llm.api:app --host 0.0.0.0 --port 8000"]
    Env["Miniconda env local-llm from environment.yaml"]
    ModelsVol[("bind mount ./models")]
    CacheVol[("bind mount ./.cache/responses")]
    ConfigFile[("read-only ./local_llm.yaml")]
    OC["OpenCode"]

    Host --> Loop
    OC --> Loop
    Loop --> Container
    Container --> Uvicorn
    Uvicorn --> Env
    Uvicorn --> ModelsVol
    Uvicorn --> CacheVol
    Uvicorn --> ConfigFile
```

## Dependency rules

- `CompositionRoot` is the only component that constructs concrete implementations.
- HTTP routes depend on application contracts, not on `llama_cpp` or filesystem details.
- I/O, clocks, randomness, subprocess calls, and retries stay at explicit edges.
- Response cache lookup happens before preflight and runtime load.
- Cache keys include request `options`; changing `n_ctx`, `n_batch`, `flash_attn`, `temperature`, `top_p`, `top_k`, `min_p`, or `repeat_penalty` produces a different key.
- `make_cache_key` receives a validated alias from the registry and a fingerprint from `weight_fingerprint`; both are non-empty by contract and are not re-checked inside the cache.
- No component reads host Python or host conda state.
- All persistent state is through the two mounted volumes and the read-only config file.

## Verified and unverified

- Verified: `docker compose config` and `docker compose --profile serve config` parse successfully with the provided Docker Compose binary.
- Verified: the Miniconda test image builds and `docker compose run --rm tests` passes all 9 response-cache tests (`Ran 9 tests ... OK`).
- Superseded: earlier host-conda cache test results and the Docker daemon HTTP 500 failure are no longer the latest evidence.
- Pending: API scaffold and integration of C1–C8 and C10 into the containerized service.
