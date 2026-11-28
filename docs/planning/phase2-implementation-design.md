# Phase 2 Implementation Design — Kubernetes Primary (Planned)

**Status:** implementation design record. The design choices below were applied on branch `phase2-kubernetes-implementation`; see `docs/deployment.md` for the committed artifact inventory and verification commands. `DECISION-26` and `AC-81`–`AC-167` in `docs/requirements.md` remain the authoritative gates.

**Authoritative scope:** `docs/requirements.md` (DECISION-26, AC-81–AC-167). Runtime settings source of truth: `local_llm.yaml`. Operating rules: `AGENTS.md`.

**Applied skills:** `tech-lead` (reductions first, one narrow public surface per module, injected collaborators, no cycles, route only changed boundaries), `architecture-engineering` (cohesive public surface, collaborators passed in, acyclic graph, I/O/time/randomness at edges, errors as contract, deletion/reuse over addition).

---

## 1. Purpose and constraints

Implement the smallest additive Phase 2 surface that makes `AC-81`–`AC-167` observable, while preserving every MVP contract:

- No existing `local_llm.yaml` key, route, error envelope, or `local_llm/` module contract changes.
- `local Compose` stays loopback-only (`127.0.0.1:8000`), unchanged.
- The 142-test MVP baseline within the 174-test gate (`docker compose run --rm tests`) stays green (`AC-161`).
- Application code stays auth-less: no API-key validation, no secrets-manager calls, no object-storage I/O in the request path (`AC-109`, `AC-129`). Gateway, K8s Secrets, and init-containers own those edges.
- One resident model instance per process; serialized generation; one process-wide lock (`AC-91`).
- GPU fail-closed: no silent CUDA→CPU fallback; CPU fallback requires explicit opt-in, which Phase 2 does not grant (`AC-87`).
- Apple Metal is documentation-only; no Metal image, no Docker/CI Metal claim (`AC-166`, `AC-167`).
- `ASSUMPTION-17`, `ASSUMPTION-18`, `ASSUMPTION-19`, `AC-21` remain open; none are silently closed (`AC-157`–`AC-160`).

**Reuse before adding (explicit):**
- `ErrorMapper` already maps `ResourceError` → `503`; placement failures reuse `ResourceError` (no new error type, no error_mapper change).
- `HfCliDownloader` is unchanged; the `hf` path for `AC-4`/`AC-5`/`AC-97`/`AC-98` is reused verbatim.
- `locked()` and `AliasDownloadLocks` are unchanged; download I/O stays outside the lifecycle lock (`AC-96`).
- `runtime.py` merge functions are unchanged; backend defaults are folded into the effective `llama_defaults` mapping at composition (`AC-88`, `AC-89`).
- `cache.py` atomic write, TTL, and LRU eviction are unchanged; per-instance isolation is an `instance_id` subdirectory (`AC-92`, `AC-93`).
- `admin_routes.py`, `chat_routes.py`, `api.py`, `composition.py` (`create_app`, `build_app` surface) stay thin; only additive collaborators are injected.

**Not added (smallest design):** server-side auth/rate limiting, cache encryption, shared/distributed cache, content-hash GGUF fingerprints, secrets-manager client, object-storage client in the request path, Metal implementation, local non-root container, async download queue, admin alias gating, cancellation endpoint.

---

## 2. Ambiguities in the acceptance criteria and conservative resolutions

| # | Ambiguity | Conservative resolution (documented here so it is not decided implicitly during implementation) |
|---|-----------|--------------------------------------------------------------------------------------------------|
| A1 | **Where does object-storage sync run?** The module table lists `ObjectStorageSync.sync`, but the deployment topology says initContainer owns sync, and security says application code never performs object-storage I/O directly. | `ObjectStorageSync` is a transport module with an injected HTTP client, injected credentials, and injected paths. It is invokable only at an edge (initContainer entrypoint or an explicit startup step), never inside a chat request and never under the lifecycle lock. `AC-94`/`AC-95`/`AC-96` are verified by unit tests that call `.sync()` directly with fakes. The production `uvicorn local_llm.api:app` path uses `hf` or `volume` only unless an operator explicitly wires sync at the edge. |
| A2 | **Can CUDA run in the CPU container image?** `AC-86`/`AC-89`/`AC-90` reference CUDA probes and VRAM, but the committed image is CPU-only (`llama-cpp-python` CPU). | Backend selection is pure config + injected probes; `select()` is testable with a fake CUDA probe. Real CUDA requires the separate CUDA image flavor (`Dockerfile.cuda`) plus the NVIDIA device plugin — not claimed in the CPU container. CPU-container tests fake a CUDA probe to exercise the fail-closed `503` path (`AC-87`) without claiming CUDA inference in that image. |
| A3 | **Does `registry.supply: object_storage` change the chat path to sync-on-demand?** `AC-94` says sync fetches the GGUF before load with an empty `models/<alias>/`. | Sync is a pre-load edge operation (initContainer or startup step), not per-request. `ModelSupply.resolve()` returns the local GGUF path; the bytes must already be present (synced by initContainer or placed on the volume). The chat path never triggers object-storage I/O. `AC-94`/`AC-95` are verified at the `ObjectStorageSync` module level with fakes. |
| A4 | **How is "cache directory derived from the injected instance id" reconciled with the existing cache dir?** | `ResponseCache(root, config, instance_id=None)`: when `instance_id` is provided, `dir = base_dir / instance_id`; when `None` (MVP/Compose), `dir = base_dir` exactly as today. Two instances with different ids get disjoint subdirectories (`AC-92`, `AC-93`). No new cache module. |
| A5 | **Where do backend defaults sit in the merge order?** Spec: `llama_defaults → backend_defaults[selected] → model options → request options`. | Do not change tested `runtime.merge_load_options`. Composition folds `backend_defaults[selected_backend]` into one effective `llama_defaults` mapping before constructing `LlamaRuntimeAdapter`; model and request options still override. `runtime.py` unchanged; cache-key "runtime" section reflects the folded mapping (`AC-88`, `AC-89`). |
| A6 | **Are `AC-107`–`AC-116` (gateway/TLS/NetworkPolicy) application code?** | No. They are Helm-chart / ingress-controller / Kubernetes concerns. The app receives no API key (`AC-109`) and writes no audit records (`AC-151`–`AC-154` are gateway audit). Verified by `helm template` / `helm install --dry-run` (`AC-131`–`AC-132`) and manifest review, not Python unit tests. |
| A7 | **Are `AC-134`–`AC-141` (Terraform) and `AC-142`–`AC-146` (supply chain) Python tests?** | No. Infrastructure / CI-platform gates, executed in platform tooling, not in `docker compose run --rm tests`. The design references them; their verification is outside the Python test container. |
| A8 | **Must `cache.scope` reject non-`per_instance`?** The validation rules say "`cache.scope` is `per_instance`"; no AC explicitly tests a bad value. | Reject anything other than `per_instance` with the field name in the error (matches the documented validation rule; default `per_instance` keeps MVP configs valid). |

**Open decisions deliberately NOT closed:** `ASSUMPTION-18` (per-instance cold loads across replicas; no sticky affinity or warm-up added), `ASSUMPTION-19` (synced weights treated as immutable; no content-hash fingerprints), `ASSUMPTION-21` (rate-limiting residual accepted; no rate limiting added). See section 10.

---

## 3. Configuration schema changes (additive)

Only new top-level sections and one new `cache` field are added. Every existing key is unchanged (`AC-85`). The YAML below is the **target schema**; it is not applied to `local_llm.yaml` until the P2-1 slice.

```yaml
# --- existing keys unchanged: server, paths, llama_defaults, generation_defaults,
# --- max_n_ctx, cache (minus scope), download, resources, models ---

deployment:
  primary_target: kubernetes        # kubernetes | compose
  local_mode: true                  # true preserves Compose loopback behavior
  terraform_reference: aws          # AWS EKS first; other providers are adapters
  ingress:
    enabled: false                  # enabled by Helm values, never by local Compose
    gateway_policy: api_key         # enforced outside the application
    api_key_secret_ref: local-llm-api-key
    publish: loopback               # Compose: loopback; Helm: public via ingress

compute:
  default_backend: cpu
  active_backends: [cpu, cuda]      # non-empty subset of {cpu, cuda}
  documented_backends: [metal]      # metal reference only; never in active_backends
  device_placement: auto            # auto | manual
  fallback: fail_closed             # CPU fallback requires explicit opt-in (not granted in P2)
  backend_defaults:
    cpu:
      n_gpu_layers: 0
    cuda:
      n_gpu_layers: 33
    metal:
      n_gpu_layers: 1               # documented reference only

registry:
  supply: hf                        # hf | object_storage | volume
  object_storage:
    provider: s3
    bucket: local-llm-models
    prefix: gguf/
    region: us-east-1
    secret_ref: model-supply-credentials
  sync:
    enabled: false                  # initContainer sync before the app starts
    immutable_weights: true
    concurrency: 4

cache:
  scope: per_instance               # NEW; existing dir/ttl_days/max_size_mb/replay_sse unchanged

security:
  non_root: true                    # cloud target only
  read_only_root: true              # cloud target only
  secrets_backend: kubernetes_secret
```

### 3.1 Validation rules (startup failures name the offending field)

- `deployment.primary_target` ∈ {`kubernetes`, `compose`} — else error names `deployment.primary_target`.
- `deployment.ingress.gateway_policy` == `api_key` — else error names `deployment.ingress.gateway_policy`.
- `compute.active_backends` is a non-empty subset of {`cpu`, `cuda`} — else error names `compute.active_backends`.
- `metal` is accepted only in `compute.documented_backends`; never in `active_backends` — else error names `compute.active_backends`.
- `compute.fallback` == `fail_closed` — else error names `compute.fallback`.
- `registry.supply` ∈ {`hf`, `object_storage`, `volume`} — else error names `registry.supply`.
- `registry.object_storage.secret_ref` is non-empty when `supply == object_storage` — else error names `registry.object_storage.secret_ref`.
- `deployment.ingress.api_key_secret_ref` is non-empty when `ingress.enabled` — else error names `deployment.ingress.api_key_secret_ref`.
- `cache.scope` == `per_instance` — else error names `cache.scope`.
- `security.secrets_backend` == `kubernetes_secret` — else error names `security.secrets_backend`.
- `compute.backend_defaults` keys ⊆ `active_backends` ∪ `documented_backends` — else error names `compute.backend_defaults`.

Existing `AC-1`–`AC-85` validation is unchanged (`AC-85`). Validation errors surface as `ValueError`/`pydantic.ValidationError` from `ConfigLoader.load()` so `build_app()` fails at startup before the app serves.

### 3.2 Effective llama.cpp option order

`llama_defaults → compute.backend_defaults[selected_backend] → model options → request options`. Implemented by folding `backend_defaults[selected_backend]` into the effective `llama_defaults` mapping at composition (section 6.3). `runtime.merge_load_options` and `runtime.merge_sampling_options` are unchanged (`AC-88`, `AC-89`).

---

## 4. Module boundaries

### 4.1 New modules (exact public contracts)

#### `local_llm/compute/backend_selection.py`

```python
"""Backend preference selection. Pure; no I/O; probes are injected."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class BackendProbe(Protocol):
    """Injected probe contract. Real implementations shell out to nvidia-smi etc."""
    def cuda_available(self) -> bool: ...
    def cuda_vram_bytes(self) -> int | None: ...


@dataclass(frozen=True)
class BackendDecision:
    backend: str            # "cpu" | "cuda"
    load_options: dict      # e.g. {"n_gpu_layers": 0} or {"n_gpu_layers": 33}
    reason: str             # human-readable selection reason (logged, never secrets)


class BackendSelector:
    """One decision per container. default_backend wins unless an active backend is probed available."""

    def __init__(self, settings: Any, probes: BackendProbe) -> None: ...
    def select(self) -> BackendDecision: ...
```

- **Public surface:** `BackendSelector(settings, probes).select() -> BackendDecision`.
- **Injected:** `settings` (compute section), `probes` (fakeable).
- **Edge:** none (pure function of config + probe results).
- **Depends on:** `Settings` contracts only. No cycles. Independent tests use fake probes (`AC-86`).

#### `local_llm/compute/device_placement.py`

```python
"""Fail-closed GPU/CPU placement validation at load time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_llm.compute.backend_selection import BackendProbe
from local_llm.preflight import ResourceError


class PlacementError(ResourceError):
    """Raised when a requested backend cannot be placed; maps to 503 via ErrorMapper."""


@dataclass(frozen=True)
class Placement:
    backend: str
    n_gpu_layers: int
    vram_ok: bool


class DevicePlacement:
    """Validates a backend against resources before a GGUF load."""

    def __init__(self, resources: Any, probes: BackendProbe) -> None: ...
    def validate(self, backend: str, model_size_bytes: int) -> Placement: ...
```

- **Public surface:** `DevicePlacement(resources, probes).validate(backend, model_size_bytes) -> Placement`.
- **Injected:** `resources` (thresholds), `probes` (fake VRAM).
- **Edge:** none (pure validation; raises `PlacementError` → `503`).
- **Behavior:** fail_closed — if `backend == "cuda"` and not probed available, raise `PlacementError` naming the backend (`AC-87`). If estimated VRAM exceeds the configured limit, raise `PlacementError` (`AC-90`). `cpu` with `n_gpu_layers: 0` always validates (`AC-88`).
- **Reuse:** `PlacementError` subclasses `ResourceError`, so `ErrorMapper` already returns `503` — no error-mapper change.

#### `local_llm/sync/object_storage.py`

```python
"""Object-storage -> volume sync transport. Edge-only; never under the lifecycle lock."""

from __future__ import annotations

from typing import Any, Callable
from pathlib import Path


class ObjectStorageError(Exception):
    """Sync failure; no partial weights are left on the volume."""


class ObjectStorageSync:
    """Idempotent bucket -> GGUF volume sync."""

    def __init__(
        self,
        *,
        http_client: Callable[..., Any],     # injected; fakeable
        credentials: Any,                    # injected; resolved by caller/infra, never in code
        bucket: str,
        prefix: str,
        region: str,
    ) -> None: ...
    def sync(self, alias: str, record: Any, destination_dir: Path) -> Path:
        """Fetch the alias's GGUF into destination_dir atomically (temp file + rename).
        Returns the GGUF path. Writes no partial bytes on failure (AC-95).
        Never touches the process-wide lifecycle lock (AC-96)."""
```

- **Public surface:** `ObjectStorageSync(...).sync(alias, record, destination_dir) -> Path`.
- **Injected:** `http_client`, `credentials`, `bucket`, `prefix`, `region`, `destination_dir`.
- **Edge:** HTTP GET (bucket), volume write (temp + rename).
- **Depends on:** nothing in the app graph. Independent tests use a fake HTTP client and a temp directory (`AC-94`, `AC-95`). Not wired into `uvicorn` request path (resolution A1/A3).

#### `local_llm/model_supply.py`

```python
"""Model supply dispatch: hf / object_storage / volume. Lock-agnostic."""

from __future__ import annotations

from typing import Any
from pathlib import Path


class ModelSupplyError(Exception):
    """Supply resolution failure."""


class ModelSupply:
    """Resolve a registry record to a local GGUF path without holding the lifecycle lock."""

    def __init__(
        self,
        *,
        supply: str,                 # "hf" | "object_storage" | "volume"
        downloader: Any,             # HfCliDownloader (hf path)
        sync: Any | None,            # ObjectStorageSync (object_storage path)
        root: Path,
    ) -> None: ...
    def resolve(self, record: Any) -> Path:
        """hf:     downloader.download (caller manages the lifecycle lock per DECISION-22).
           object_storage: sync then path (sync ran at the edge; see A1/A3).
           volume:   existence check only; error if absent.
           Never holds the lifecycle lock itself (AC-96)."""
```

- **Public surface:** `ModelSupply(...).resolve(record) -> Path`.
- **Injected:** `supply`, `downloader`, `sync`, `root`.
- **Edge:** delegates to `downloader.download` / `sync.sync` / `exists`.
- **Depends on:** `HfCliDownloader` (reused unchanged) and `ObjectStorageSync`. No lifecycle lock parameter — lock-agnostic by design (`AC-96`).
- **Independent tests:** fake downloader and fake sync; assert `hf` path unchanged when `supply == hf` (`AC-97`, `AC-98`).

### 4.2 Changed modules (additive, backward compatible)

#### `local_llm/config.py`

- New frozen pydantic section models: `DeploymentSettings`, `IngressSettings`, `ComputeSettings`, `BackendDefaults`, `RegistrySettings`, `ObjectStorageSettings`, `SyncSettings`, `SecuritySettings`.
- `CacheConfig` gains `scope: str = "per_instance"` with a validator rejecting anything but `per_instance` (`AC-81`, cache.scope rule).
- `Settings` gains optional fields `deployment`, `compute`, `registry`, `security` (default to Phase 2-safe values so MVP configs without them validate unchanged — `AC-85`).
- Model validators enforce the section 3.1 rules, each naming the offending field (`AC-82`, `AC-83`, `AC-84`).
- **Public surface unchanged:** `ConfigLoader.load() -> Settings`. Additive only.

#### `local_llm/cache.py`

- `ResponseCache.__init__(self, config=None, *, root=None, clock=None, instance_id=None)`.
- `_resolve_dir(instance_id)` returns `base_dir / instance_id` when `instance_id` is given, else `base_dir` (MVP behavior).
- **Public surface unchanged:** `get`, `put`, `replay_sse`, `evict`. Additive `instance_id` parameter (`AC-92`, `AC-93`).

#### `local_llm/composition.py`

- `Services` dataclass gains optional fields: `backend_decision`, `placement`, `model_supply`, `object_storage_sync`, `instance_id`, `effective_llama_defaults`.
- `build_app()`:
  1. Loads settings (new sections validated — `AC-81`–`AC-85`).
  2. Constructs `BackendSelector(settings, probes).select()` → `backend_decision` (injected probe; fakeable in tests — `AC-86`).
  3. Folds `backend_defaults[selected_backend]` into `effective_llama_defaults` (`llama_defaults → backend_defaults → model options → request options` at runtime merge — `AC-88`, `AC-89`).
  4. Constructs `DevicePlacement(resources, probes)` → `placement`.
  5. Constructs `ModelSupply(supply=..., downloader=..., sync=..., root=...)` → `model_supply`.
  6. Constructs `ResponseCache(..., instance_id=instance_id)` — `instance_id` from optional injected value/env `LOCAL_LLM_INSTANCE_ID` (absent in Compose → MVP behavior).
  7. Startup downloads (`_download_configured_models`) run only when `registry.supply == "hf"`; the `hf` path is unchanged (`AC-97`). When `supply != "hf"`, weights are expected on the volume (synced by initContainer) and startup does not invoke the HF CLI (`AC-94`/`AC-95` handled at the edge).
- `create_app()` signature unchanged (`include_admin`, `lifespan`).
- **Dependency graph:** `CompositionRoot` remains the only constructor of concrete implementations; graph stays acyclic.

#### `local_llm/chat_routes.py` (one additive call in the load path)

- In `_ensure_loaded`, before `services.runtime.load(...)`, call `services.placement.validate(selected_backend, model_size_bytes)` when `placement` is configured. Failure raises `PlacementError` → `ErrorMapper` → `503` naming the backend (`AC-87`, `AC-90`).
- Cache-key "runtime" load-options merge uses `services.effective_llama_defaults` instead of `services.settings.llama_defaults` so backend defaults are part of the key (`AC-88`, `AC-89`).
- Routes remain thin; no auth, no secrets, no object-storage (`AC-109`, `AC-129`).

### 4.3 Unchanged modules (reused as-is)

| Module | Role in Phase 2 | Reused contract |
|--------|----------------|-----------------|
| `local_llm/runtime.py` | `LlamaRuntimeAdapter` load/generate; one resident instance (`AC-91`) | Unchanged; receives `effective_llama_defaults` at construction |
| `local_llm/locking.py` | `locked()`, `AliasDownloadLocks` | Unchanged; download I/O outside lifecycle lock (`AC-62`–`AC-66`, `AC-96`) |
| `local_llm/downloader.py` | `HfCliDownloader` hf path | Unchanged (`AC-97`, `AC-98`) |
| `local_llm/preflight.py` | `ResourcePreflight` disk/RAM fail-closed | Unchanged |
| `local_llm/error_mapper.py` | Stable sanitized envelopes; `ResourceError` → `503` | Unchanged; `PlacementError` reuses it |
| `local_llm/streaming.py` | `CompletionAccumulator`, `sse_data` | Unchanged |
| `local_llm/admin_routes.py` | add/unload/remove | Unchanged (`AC-98` hf admin path) |
| `local_llm/api.py` | `app = build_app()` | Unchanged entry point |

### 4.4 Dependency graph (acyclic)

```
ConfigLoader (C2)  <--- Settings contracts
   |
CompositionRoot (C1) constructs:
  - BackendSelector  (reads Settings.compute; injected BackendProbe)
  - DevicePlacement  (reads Settings.resources; injected BackendProbe; raises ResourceError)
  - ModelSupply      (delegates to HfCliDownloader / ObjectStorageSync)
  - ObjectStorageSync (injected http_client + credentials + paths; no app deps)
  - ResponseCache    (injected instance_id, clock, root)
  - LlamaRuntimeAdapter (effective_llama_defaults; lock; factory)
  - ErrorMapper, Registry, ChatRoutes, AdminRoutes
```

Rules preserved: `CompositionRoot` is the only concrete constructor; routes depend on contracts, not `llama_cpp` or filesystem details; I/O, clocks, subprocess, and retries stay at edges; no cycles; no sibling-internal access; no shared mutable state; download/sync never hold the lifecycle lock.

---

## 5. Composition wiring summary

| Concern | Wiring location | Injected at boundary |
|---------|----------------|----------------------|
| Backend selection | `build_app()` startup | `BackendProbe` (fake in tests; nvidia-smi shell-out deferred to the CUDA flavor) |
| Device placement | chat load path, before `runtime.load()` | `ResourceSettings`, `BackendProbe` |
| Effective llama defaults | `build_app()`, passed to `LlamaRuntimeAdapter` and chat cache-key merge | folded `backend_defaults[selected]` |
| Model supply dispatch | `_resolve_path` (chat) and startup downloads | `HfCliDownloader`, `ObjectStorageSync`, `root` |
| Object-storage sync | initContainer/edge only (never chat) | `http_client`, `credentials`, `bucket/prefix/region` |
| Per-instance cache | `ResponseCache` construction | `instance_id` (optional; absent in Compose) |
| Startup download gating | `_download_configured_models` | `registry.supply == "hf"` → hf path; otherwise skip |

The `hf` startup path and admin download path are byte-for-byte unchanged (`AC-97`, `AC-98`). Object-storage sync, when enabled, runs before the app process serves (initContainer) or at an explicit startup edge — never under the lifecycle lock and never in a chat handler.

---

## 6. Artifact layout

All paths are relative to the repo root. Nothing is committed into the image except code/config/locks; weights and cache are volumes only (`AC-101`–`AC-103`).

```
repo root
├── Dockerfile                      (existing: CPU flavor; digest-pinned miniconda base +
│                                     environment-linux-64.lock; references cpu digest)
├── Dockerfile.cuda                 (NEW: digest-pinned base + environment-cuda-64.lock;
│                                     generated inside Docker; never committed weights/token/cache)
├── environment-linux-64.lock       (existing, committed; linux-64 explicit SHA-256 lock)
├── environment-cuda-64.lock        (NEW: generated inside Docker for the CUDA flavor;
│                                     committed for the promoted cuda digest; never modified by
│                                     the cpu lock; no unpinned pip install --upgrade pip)
├── docker-compose.yml              (existing: CPU flavor, loopback-only publish 127.0.0.1:8000,
│                                     optional private.txt env_file required:false;
│                                     references the cpu image digest)
├── helm/
│   └── local-llm/
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── templates/
│       │   ├── _helpers.tpl
│       │   ├── configmap.yaml           (local_llm.yaml mounted read-only as ConfigMap;
│       │                                  rendered values come from local_llm.yaml — AC-133)
│       │   ├── deployment.yaml          (pod: runAsNonRoot, readOnlyRootFilesystem,
│       │                                  allowPrivilegeEscalation:false, capabilities.drop ALL,
│       │                                  seccomp RuntimeDefault; writable mounts only models/,
│       │                                  .cache/responses/, /tmp; image by digest;
│       │                                  initContainer for object-storage sync when
│       │                                  registry.sync.enabled — AC-117–AC-122, AC-96)
│       │   ├── service.yaml             (ClusterIP; admin routes not exposed publicly)
│       │   ├── ingress.yaml             (TLS 1.2+, HTTP->HTTPS redirect; gateway api_key
│       │                                  policy gated by ingress.enabled; public paths are
│       │                                  /health and /v1/models/{alias}/chat/completions only;
│       │                                  /v1/models and /admin/models* excluded — AC-104–AC-116)
│       │   ├── networkpolicy.yaml       (deny all direct pod access; allow ingress only — AC-114)
│       │   └── secrets.yaml             (K8s Secret refs for HF_TOKEN, api key,
│       │                                  object-store credentials — never values — AC-123–AC-130)
├── terraform/
│   └── aws/
│       ├── versions.tf              (pinned provider/module versions — AC-139, AC-140)
│       ├── main.tf                  (EKS CPU + GPU node groups, S3 weights bucket,
│       │                                  K8s Secret refs, ingress, network policies — AC-134–AC-138)
│       ├── variables.tf
│       └── outputs.tf
│                                   (terraform plan repeatable/empty — AC-141; executed in
│                                   platform CI, not the Python test container — A7)
```

- Compose and Helm reference the **same digest** for the selected flavor (`AC-104`).
- Helm renders a public ingress **only when explicitly enabled** (`AC-106`).
- Compose still publishes only `127.0.0.1:8000` (`AC-105`).
- No weights, token, or cache in any image layer (`AC-101`–`AC-103`, `AC-127`).
- Image signature verification, SBOM archive, and vulnerability scan gate (`AC-144`–`AC-146`) are CI/platform supply-chain gates outside this design's application scope.

---

## 7. Test strategy

All tests run inside Docker via `docker compose run --rm tests` (`AC-161`, `AC-162`). No host Python/conda/uvicorn.

### 7.1 New test files (each independent, using fakes)

| Test file | Covers | Fakes used |
|-----------|--------|------------|
| `tests/test_config_loader_phase2.py` | `AC-81`–`AC-85` | fake YAML reader; invalid `active_backends`/`supply`/`fallback` name the field |
| `tests/test_compute_backend_selection.py` | `AC-86` | fake `BackendProbe` (cuda available / not available) |
| `tests/test_compute_device_placement.py` | `AC-87`–`AC-90` | fake `BackendProbe`; fake resource thresholds; assert `PlacementError` → 503 naming backend |
| `tests/test_sync_object_storage.py` | `AC-94`–`AC-96` | fake HTTP client (returns bytes / raises); temp dir; assert atomic write + no partial bytes on failure; no lifecycle lock parameter |
| `tests/test_model_supply.py` | `AC-94`–`AC-98` | fake downloader + fake sync; assert `hf` path unchanged and no lock held |
| `tests/test_cache_instance_id.py` | `AC-92`, `AC-93` | injected clock + temp paths; two instance ids → disjoint dirs and no shared entries |

### 7.2 Fakes contract

- Fake backend probes: `cuda_available() -> bool`, `cuda_vram_bytes() -> int | None`.
- Fake object-storage HTTP: returns GGUF bytes for a key, or raises `ObjectStorageError` on failure (assert no partial file left).
- Fake clock: fixed `datetime` for TTL/last-accessed determinism.
- Injected paths: `tmp_path`-based roots under the container workspace, never `models/` or `.cache/responses/` committed.

### 7.3 Observability / security log contract tests (`AC-147`–`AC-150`)

Capture application log output and assert it contains no token value, no prompt text, no filesystem path, no stack trace. This is an additive assertion in existing log-capture style; no new module.

### 7.4 Gates that stay outside the Python test container (`A7`)

- `helm template` / `helm install --dry-run` (`AC-131`–`AC-132`).
- `terraform plan` CPU/GPU/S3/ingress/network-policy, repeat empty, pinned providers/modules (`AC-134`–`AC-141`).
- Image digest reference parity between Compose and Helm (`AC-104`, `AC-142`, `AC-143`).
- Supply-chain signature/SBOM/vuln-scan (`AC-144`–`AC-146`).

These are platform/CI gates referenced by the design, not Python unit tests.

### 7.5 Regression and hygiene (`AC-163`–`AC-165`)

- `git check-ignore -v private.txt models/ .cache/responses/` passes.
- No model weights or cache files staged/committed.
- Full 142-test MVP suite passes alongside new Phase 2 tests.

---

## 8. Implementation slices (each Docker-testable, sequenced)

Each slice waits for the previous slice's observable gates; none changes MVP behavior. Implementation of each slice should be delegated to a developer subagent, with conditional specialist review (Security Engineer for `AC-107`–`AC-130`; Delivery Manager for Terraform/Helm; Tech Lead for module boundaries).

| Slice | Goal | Acceptance gate (observable in Docker) | Primary AC |
|-------|------|----------------------------------------|------------|
| **P2-0** | Approve Phase 2; record open assumptions and decision gates in this document; no code. | `ASSUMPTION-18`/`19`/`21` recorded open; deferral documented | `AC-155`–`AC-160`, `AC-166`–`AC-167` |
| **P2-1** | Add `deployment`, `compute`, `registry` (supply/object_storage/sync), `cache.scope`, `security` sections and validation. | `ConfigLoader` accepts the sections; unknown `active_backends`/`supply`/bad `fallback` fail naming the field; existing config still validates | `AC-81`–`AC-85` |
| **P2-2** | Add `compute/backend_selection.py` and `compute/device_placement.py`; fail-closed GPU placement. | CUDA probe → decision `cuda`; no CUDA + fail_closed → `503` naming backend; `cpu` → `n_gpu_layers: 0`; CUDA VRAM over limit → `503` | `AC-86`–`AC-90` |
| **P2-3** | Per-instance cache isolation; confirm one resident instance per process. | `ResponseCache(instance_id=...)` dir derivation; two instance ids do not share entries; runtime still one instance | `AC-91`–`AC-93` |
| **P2-4** | Add `sync/object_storage.py` and `model_supply.py`; dispatch hf/object_storage/volume outside the lifecycle lock. | `object_storage` sync fetches before load (fake); sync failure writes no partial weights; sync/download holds no lifecycle lock; `hf` startup and admin behavior unchanged | `AC-94`–`AC-98` |
| **P2-5** | Add `Dockerfile.cuda` and `environment-cuda-64.lock`; cloud pod hardening shapes. | CPU image builds from digest-pinned base + linux-64 lock; CUDA image builds from digest-pinned base + its own lock; no weights/token/cache in image layers; Helm pod spec has non-root/read-only/dropped caps/RuntimeDefault/writable mounts limited to models/.cache//tmp | `AC-99`–`AC-103`, `AC-117`–`AC-122` |
| **P2-6** | Helm gateway TLS + API-key ingress; public paths only `/health` and per-alias chat; admin internal; direct pod denied. | `helm template` / `helm install --dry-run` render without errors; ingress off by default; public ingress exposes only `/health` and `/v1/models/{alias}/chat/completions`; NetworkPolicy denies direct pod; TLS 1.2+ and HTTP→HTTPS shapes | `AC-104`–`AC-116` |
| **P2-7** | K8s Secret / workload-identity wiring; no secret values in app surface. | `HF_TOKEN`, api-key ref, object-store creds reference K8s Secrets only; no secret in `local_llm.yaml`, image layer, CLI argument, app logs, or shared manifest output; app reads env only | `AC-123`–`AC-130` |
| **P2-8** | Ship Helm chart + AWS Terraform reference module. | Same digest referenced by Compose and Helm; `terraform plan` provisions EKS CPU/GPU node groups, S3 bucket, ingress, network policies; providers/modules pinned; repeated plan empty | `AC-104`, `AC-131`–`AC-141` |
| **P2-9** | Supply-chain, observability, and full verification. | App logs contain no token/prompt/path/stack; 142 MVP tests + new Phase 2 tests pass in Docker; ignore checks pass; no weights/cache staged; Metal documented-only with no Docker/CI claim | `AC-142`–`AC-154`, `AC-161`–`AC-167` |

---

## 9. Deferred, rejected, and open (explicit, not silently closed)

- **Deferred:** application-level auth and rate limiting, cache encryption/shorter TTL, shared/distributed cache, content-hash GGUF fingerprints, local Compose non-root, Metal implementation/verification, AMD ROCm, cloud-managed inference endpoints, concurrent generation, multi-model residency, async download queue, admin alias gating, gated/private HF repos, multi-architecture locks.
- **Rejected for Phase 2:** baking weights into images, public unauthenticated cloud exposure, direct pod exposure, Metal cloud container image, silent GPU→CPU fallback, secret values in YAML/images/CLI/logs/manifests.
- **Open assumptions (not closed):** `ASSUMPTION-17` (host trust / local auth/rate limiting/non-root/cache encryption), `ASSUMPTION-18` (per-instance cold loads across replicas; no sticky affinity), `ASSUMPTION-19` (immutable synced weights; `size:mtime` fingerprint), `ASSUMPTION-21` (rate-limiting residual; gateway-side limits preferred if added later).
- **Open security findings carried forward:** `SEC-G-02` (cache dir mode), `SEC-G-03` (admin remove cache purge), `SEC-G-04` (server-side body/token cap) remain recommendations only; no code added here.

---

## 10. Definition of done mapping (AC-81–AC-167)

| AC range | Where addressed | Verified by |
|----------|----------------|-------------|
| `AC-81`–`AC-85` | `config.py` new sections + validation; `build_app()` | `tests/test_config_loader_phase2.py` + full suite |
| `AC-86`–`AC-90` | `compute/backend_selection.py`, `compute/device_placement.py`; chat load-path placement call | `tests/test_compute_*.py` (fake probes) |
| `AC-91`–`AC-93` | `ResponseCache(instance_id=...)`; runtime unchanged (one instance) | `tests/test_cache_instance_id.py` + residency tests |
| `AC-94`–`AC-98` | `sync/object_storage.py`, `model_supply.py`, `build_app()` supply gating | `tests/test_sync_object_storage.py`, `tests/test_model_supply.py` |
| `AC-99`–`AC-103` | `Dockerfile.cuda`, `environment-cuda-64.lock`; image-layer hygiene | Docker builds inside Docker; no weights/token/cache in image |
| `AC-104`–`AC-116` | `helm/local-llm/templates/*` (ingress, networkpolicy, service, deployment) | `helm template` / `helm install --dry-run` |
| `AC-117`–`AC-122` | Helm pod spec securityContext + mounts | `helm template` render inspection |
| `AC-123`–`AC-130` | Helm `secrets.yaml` references; app reads env only | Manifest review; app log capture tests (`AC-147`–`AC-150`) |
| `AC-131`–`AC-134` | Helm chart rendering; config values from `local_llm.yaml` | `helm template` / dry-run |
| `AC-135`–`AC-141` | `terraform/aws/*` | `terraform plan` in platform CI (not Python container) |
| `AC-142`–`AC-146` | CI supply-chain pipeline (digest, signature, SBOM, vuln scan) | Platform CI; referenced, not Python tests |
| `AC-147`–`AC-150` | App log contract (no token/prompt/path/stack) | Log-capture assertions in tests |
| `AC-151`–`AC-154` | Gateway audit (not app code) | Gateway/ingress configuration; documented as out-of-app scope |
| `AC-155`–`AC-160` | This document (deferral and open-assumption records) | Documentation gate (P2-0) |
| `AC-161`–`AC-165` | `docker compose run --rm tests` (174 tests); ignore checks; no weights/cache staged | Docker test gate |
| `AC-166`–`AC-167` | This document + `docs/architecture.md` Phase 2 section (implemented with platform gates) | Documentation gate |

---

## 11. Notes for the implementing developer

1. Do not modify `runtime.py`, `locking.py`, `downloader.py`, `preflight.py`, `error_mapper.py`, `streaming.py`, `admin_routes.py`, or `api.py` — reuse them. Additive changes only to `config.py`, `cache.py` (`instance_id`), `composition.py`, and `chat_routes.py` (one placement call).
2. Every new module must have independent tests with injected fakes inside Docker (`docker compose run --rm tests`).
3. `build_app()` must remain the single composition root; no module may construct its own concrete `HfCliDownloader`, `LlamaRuntimeAdapter`, or `ResponseCache`.
4. Keep the `hf` download path byte-for-byte unchanged when `registry.supply == "hf"` (`AC-97`, `AC-98`).
5. Do not close `ASSUMPTION-17`, `ASSUMPTION-18`, `ASSUMPTION-19`, or `ASSUMPTION-21` in code or docs.
6. Any slice that would cross a boundary without an explicit contract (e.g., adding auth, encryption, shared cache, or Metal) must stop and be re-scoped by the tech lead.
